"""Trainer method."""

import os
from functools import partial
from typing import Callable, Dict, Optional, Tuple

import haiku as hk
import jax
import jax.numpy as jnp
import jraph
import optax
from jax import vmap
from torch.utils.data import DataLoader
from wandb.wandb_run import Run

from lagrangebench.case_setup import CaseSetupFn
from lagrangebench.data import H5Dataset
from lagrangebench.data.utils import numpy_collate
from lagrangebench.defaults import defaults
from lagrangebench.evaluate import MetricsComputer, averaged_metrics, eval_rollout
from lagrangebench.utils import (
    LossConfig,
    PushforwardConfig,
    broadcast_from_batch,
    broadcast_to_batch,
    get_kinematic_mask,
    load_haiku,
    save_haiku,
    set_seed,
)

from .strats import push_forward_build, push_forward_sample_steps


@partial(jax.jit, static_argnames=["model_fn", "loss_weight"])
def _mse(
    params: hk.Params,
    state: hk.State,
    features: Dict[str, jnp.ndarray],
    particle_type: jnp.ndarray,
    target: jnp.ndarray,
    model_fn: Callable,
    loss_weight: LossConfig,
):
    pred, state = model_fn(params, state, (features, particle_type))
    # check active (non zero) output shapes
    keys = list(set(loss_weight.nonzero) & set(pred.keys()))
    assert all(target[k].shape == pred[k].shape for k in keys)
    # particle mask
    non_kinematic_mask = jnp.logical_not(get_kinematic_mask(particle_type))
    num_non_kinematic = non_kinematic_mask.sum()
    # loss components
    losses = []
    for t in keys:
        losses.append((loss_weight[t] * (pred[t] - target[t]) ** 2).sum(axis=-1))
    total_loss = jnp.array(losses).sum(0)
    total_loss = jnp.where(non_kinematic_mask, total_loss, 0)
    total_loss = total_loss.sum() / num_non_kinematic

    return total_loss, state


@partial(jax.jit, static_argnames=["loss_fn", "opt_update"])
def _update(
    params: hk.Module,
    state: hk.State,
    features_batch: Tuple[jraph.GraphsTuple, ...],
    target_batch: Tuple[jnp.ndarray, ...],
    particle_type_batch: Tuple[jnp.ndarray, ...],
    opt_state: optax.OptState,
    loss_fn: Callable,
    opt_update: Callable,
) -> Tuple[float, hk.Params, hk.State, optax.OptState]:
    value_and_grad_vmap = vmap(
        jax.value_and_grad(loss_fn, has_aux=True), in_axes=(None, None, 0, 0, 0)
    )
    (loss, state), grads = value_and_grad_vmap(
        params, state, features_batch, particle_type_batch, target_batch
    )

    # aggregate over the first (batch) dimension of each leave element
    grads = jax.tree_map(lambda x: x.sum(axis=0), grads)
    state = jax.tree_map(lambda x: x.sum(axis=0), state)
    loss = jax.tree_map(lambda x: x.mean(axis=0), loss)

    updates, opt_state = opt_update(grads, opt_state, params)
    new_params = optax.apply_updates(params, updates)

    return loss, new_params, state, opt_state


def Trainer(
    model: hk.Module,
    case: CaseSetupFn,
    dataset_train: H5Dataset,
    dataset_eval: H5Dataset,
    metrics: Optional[Dict] = None,
    seed: int = defaults.seed,
):
    """
    Trainer builder function. Returns a function that trains the model on the given
    case and dataset_train, evaluating it on dataset_eval with the specified metrics.

    Args:
        model: (Transformed) Haiku model.
        case: Case setup class.
        dataset_train: Training dataset.
        dataset_eval: Validation dataset.
        metrics: Metrics to evaluate the model on.
        seed: Random seed.

    Returns:
        Configured training function.
    """

    base_key, seed_worker, generator = set_seed(seed)

    metrics_computer = MetricsComputer(
        metrics,
        dist_fn=case.displacement,
        metadata=dataset_train.metadata,
        input_seq_length=dataset_train.input_seq_length,
    )

    def _train(
        lr_start: float = defaults.lr_start,
        step_max: int = defaults.step_max,
        batch_size: int = defaults.batch_size,
        pushforward: Optional[PushforwardConfig] = defaults.pushforward,
        noise_std: float = defaults.noise_std,
        params: Optional[hk.Params] = None,
        state: Optional[hk.State] = None,
        store_checkpoint: Optional[str] = None,
        load_checkpoint: Optional[str] = None,
        wandb_run: Optional[Run] = None,
        **kwargs,
    ):
        """
        Training function. Trains and evals the model on the given case and dataset, and
        saves the model checkpoints and best models.

        Args:
            lr_start: Initial learning rate.
            step_max: Maximum number of training steps.
            batch_size: Training batch size.
            pushforward: Pushforward configuration.
            noise_std: Noise standard deviation for the GNS-style noise.
            params: Optional model parameters. If provided, training continues from it.
            state: Optional model state.
            store_checkpoint: Checkpoints destination. Without it params aren't saved.
            load_checkpoint: Initial checkpoint directory. If provided resumes training.
            wandb_run: Wandb run.

        Keyword Args:
            lr_end: Final learning rate.
            lr_steps: Number of steps to reach the final learning rate.
            lr_decay_rate: Learning rate decay rate.
            input_seq_length: Input sequence length. Default is 6.
            n_rollout_steps: Number of autoregressive rollout steps.
            eval_n_trajs: Number of trajectories to evaluate.
            rollout_dir: Rollout directory.
            out_type: Output type.
            log_steps: Wandb/screen logging frequency.
            eval_steps: Evaluation and checkpointing frequency.
            loss_weight: Loss weight object.

        Returns:
            Tuple containing the final model parameters, state and optimizer state.
        """
        # dataloaders
        loader_train = DataLoader(
            dataset=dataset_train,
            batch_size=batch_size,
            shuffle=True,
            num_workers=4,
            collate_fn=numpy_collate,
            drop_last=True,
            worker_init_fn=seed_worker,
            generator=generator,
        )
        loader_eval = DataLoader(
            dataset=dataset_eval,
            batch_size=1,
            collate_fn=numpy_collate,
            worker_init_fn=seed_worker,
            generator=generator,
        )

        # trajectory and rollout config
        input_seq_length = kwargs.get("input_seq_length", defaults.input_seq_length)
        n_rollout_steps = kwargs.get("n_rollout_steps", defaults.n_rollout_steps)
        eval_n_trajs = kwargs.get("eval_n_trajs", defaults.eval_n_trajs)
        rollout_dir = kwargs.get("rollout_dir", defaults.rollout_dir)
        out_type = kwargs.get("out_type", defaults.out_type)

        log_steps = kwargs.get("log_steps", defaults.log_steps)
        eval_steps = kwargs.get("eval_steps", defaults.eval_steps)

        assert n_rollout_steps <= dataset_eval.subsequence_length - input_seq_length, (
            "If you want to evaluate the loss on more than the ground truth trajectory "
            "length, then use the --eval_n_more_steps argument."
        )
        assert eval_n_trajs <= len(
            loader_eval
        ), "eval_n_trajs must be <= len(loader_valid)"

        # learning rate decays from lr_start to lr_end over lr_steps exponentially
        lr_scheduler = optax.exponential_decay(
            init_value=lr_start,
            transition_steps=kwargs.get("lr_steps", defaults.lr_steps),
            decay_rate=kwargs.get("lr_decay_rate", defaults.lr_decay_rate),
            end_value=kwargs.get("lr_end", defaults.lr_end),
        )
        # optimizer
        opt_init, opt_update = optax.adamw(
            learning_rate=lr_scheduler, weight_decay=1e-8
        )

        # Precompile model for evaluation
        model_apply = jax.jit(model.apply)

        # loss and update functions
        loss_weight = kwargs.get("loss_weight", LossConfig())
        loss_fn = partial(_mse, model_fn=model_apply, loss_weight=loss_weight)
        update_fn = partial(_update, loss_fn=loss_fn, opt_update=opt_update)

        # init values
        pos_input_and_target, particle_type = next(iter(loader_train))
        sample = (pos_input_and_target[0], particle_type[0])
        key, features, _, neighbors = case.allocate(base_key, sample)

        if params is not None:
            # continue training from params
            if state is None:
                state = {}
        elif load_checkpoint:
            # continue training from checkpoint
            params, state, opt_state, step = load_haiku(load_checkpoint)
        else:
            # initialize new model
            key, subkey = jax.random.split(key, 2)
            params, state = model.init(subkey, (features, particle_type[0]))

        if load_checkpoint is None:
            opt_state = opt_init(params)
            step = 0

        # create new checkpoint directory
        if store_checkpoint is not None:
            os.makedirs(store_checkpoint, exist_ok=True)
            os.makedirs(os.path.join(store_checkpoint, "best"), exist_ok=True)

        preprocess_vmap = jax.vmap(case.preprocess, in_axes=(0, 0, None, 0, None))
        push_forward = push_forward_build(model_apply, case)
        push_forward_vmap = jax.vmap(push_forward, in_axes=(0, 0, 0, 0, None, None))

        # prepare for batch training.
        keys = jax.random.split(key, loader_train.batch_size)
        neighbors_batch = broadcast_to_batch(neighbors, loader_train.batch_size)

        # start training
        while step < step_max:
            for raw_batch in loader_train:
                # numpy to jax
                raw_batch = jax.tree_map(lambda x: jnp.array(x), raw_batch)

                key, unroll_steps = push_forward_sample_steps(key, step, pushforward)
                # target computation incorporates the sampled number pushforward steps
                keys, features_batch, target_batch, neighbors_batch = preprocess_vmap(
                    keys,
                    raw_batch,
                    noise_std,
                    neighbors_batch,
                    unroll_steps,
                )
                # unroll for push-forward steps
                _current_pos = raw_batch[0][:, :, :input_seq_length]
                for _ in range(unroll_steps):
                    _current_pos, neighbors_batch, features_batch = push_forward_vmap(
                        features_batch,
                        _current_pos,
                        raw_batch[1],
                        neighbors_batch,
                        params,
                        state,
                    )

                if neighbors_batch.did_buffer_overflow.sum() > 0:
                    # check if the neighbor list is too small for any of the samples
                    # if so, reallocate the neighbor list
                    ind = jnp.argmax(neighbors_batch.did_buffer_overflow)
                    edges_ = neighbors_batch.idx[ind].shape
                    print(f"Reallocate neighbors list {edges_} at step {step}")
                    sample = broadcast_from_batch(raw_batch, index=ind)
                    _, _, _, nbrs = case.allocate(keys[0], sample)
                    print(f"To list {nbrs.idx.shape}")

                    neighbors_batch = broadcast_to_batch(nbrs, loader_train.batch_size)

                    # To run the loop N times even if sometimes
                    # did_buffer_overflow > 0 we directly return to the beginning
                    continue

                loss, params, state, opt_state = update_fn(
                    params=params,
                    state=state,
                    features_batch=features_batch,
                    target_batch=target_batch,
                    particle_type_batch=raw_batch[1],
                    opt_state=opt_state,
                )

                if step % log_steps == 0:
                    loss.block_until_ready()
                    if wandb_run:
                        wandb_run.log({"train/loss": loss.item()}, step)
                    else:
                        step_str = str(step).zfill(len(str(int(step_max))))
                        print(f"{step_str}, train/loss: {loss.item():.5f}.")

                if step % eval_steps == 0 and step > 0:
                    nbrs = broadcast_from_batch(neighbors_batch, index=0)
                    eval_metrics, nbrs = eval_rollout(
                        case=case,
                        metrics_computer=metrics_computer,
                        model_apply=model_apply,
                        params=params,
                        state=state,
                        neighbors=nbrs,
                        loader_eval=loader_eval,
                        n_rollout_steps=n_rollout_steps,
                        n_trajs=eval_n_trajs,
                        rollout_dir=rollout_dir,
                        out_type=out_type,
                    )

                    metrics = averaged_metrics(eval_metrics)
                    metadata_ckp = {
                        "step": step,
                        "loss": metrics["val/loss"],
                    }
                    if store_checkpoint is not None:
                        save_haiku(
                            store_checkpoint, params, state, opt_state, metadata_ckp
                        )

                    if wandb_run:
                        wandb_run.log(metrics, step)
                    else:
                        print(metrics)

                step += 1
                if step == step_max:
                    break

        return params, state, opt_state

    return _train
