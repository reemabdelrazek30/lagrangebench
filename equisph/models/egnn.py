"""E(3) equivariant GNN. Model + feature transform, everything in one file."""
from argparse import Namespace
from typing import Any, Callable, Dict, Iterable, Optional, Tuple, Type

import haiku as hk
import jax
import jax.numpy as jnp
import jraph
from jax.tree_util import Partial
from jax_md import space

from equisph.case_setup.features import NodeType

from .base import BaseModel


class LinearXav(hk.Linear):
    """Linear layer with Xavier init. Avoid distracting 'w_init' everywhere."""

    def __init__(
        self,
        output_size: int,
        with_bias: bool = True,
        w_init: Optional[hk.initializers.Initializer] = None,
        b_init: Optional[hk.initializers.Initializer] = None,
        name: Optional[str] = None,
    ):
        if w_init is None:
            w_init = hk.initializers.VarianceScaling(1.0, "fan_avg", "uniform")
        super().__init__(output_size, with_bias, w_init, b_init, name)


class MLPXav(hk.nets.MLP):
    """MLP layer with Xavier init. Avoid distracting 'w_init' everywhere."""

    def __init__(
        self,
        output_sizes: Iterable[int],
        with_bias: bool = True,
        w_init: Optional[hk.initializers.Initializer] = None,
        b_init: Optional[hk.initializers.Initializer] = None,
        activation: Optional[hk.initializers.Initializer] = None,
        activate_final: bool = False,
        name: Optional[str] = None,
    ):
        if w_init is None:
            w_init = hk.initializers.VarianceScaling(1.0, "fan_avg", "uniform")
        if not with_bias:
            b_init = None
        super().__init__(
            output_sizes,
            w_init,
            b_init,
            with_bias,
            activation,
            activate_final,
            name,
        )


class EGNNLayer(hk.Module):
    """EGNN layer.

    Args:
        layer_num: layer number
        hidden_size: hidden size
        output_size: output size
        blocks: number of blocks in the node and edge MLPs
        act_fn: activation function
        pos_aggregate_fn: position aggregation function
        msg_aggregate_fn: message aggregation function
        residual: whether to use residual connections
        attention: whether to use attention
        normalize: whether to normalize the coordinates
        tanh: whether to use tanh in the position update
        dt: position update step size
        eps: small number to avoid division by zero
    """

    def __init__(
        self,
        layer_num: int,
        hidden_size: int,
        output_size: int,
        blocks: int = 1,
        act_fn: Callable = jax.nn.silu,
        pos_aggregate_fn: Optional[Callable] = jraph.segment_sum,
        msg_aggregate_fn: Optional[Callable] = jraph.segment_sum,
        residual: bool = True,
        attention: bool = False,
        normalize: bool = False,
        tanh: bool = False,
        dt: float = 0.001,
        eps: float = 1e-8,
    ):
        super().__init__(f"layer_{layer_num}")

        self.pos_aggregate_fn = pos_aggregate_fn
        self.msg_aggregate_fn = msg_aggregate_fn
        self._residual = residual
        self._normalize = normalize
        self._eps = eps

        # message network
        self._edge_mlp = MLPXav(
            [hidden_size] * blocks + [hidden_size],
            activation=act_fn,
            activate_final=True,
        )

        # update network
        self._node_mlp = MLPXav(
            [hidden_size] * blocks + [output_size],
            activation=act_fn,
            activate_final=False,
        )

        # position update network
        net = [LinearXav(hidden_size)] * blocks
        # NOTE: from https://github.com/vgsatorras/egnn/blob/main/models/gcl.py#L254
        a = dt * jnp.sqrt(6 / hidden_size)
        net += [
            act_fn,
            LinearXav(1, with_bias=False, w_init=hk.initializers.UniformScaling(a)),
        ]
        if tanh:
            net.append(jax.nn.tanh)
        self._pos_correction_mlp = hk.Sequential(net)

        # velocity integrator network
        net = [LinearXav(hidden_size)] * blocks
        a = dt * jnp.sqrt(6 / hidden_size)
        net += [
            act_fn,
            LinearXav(1, with_bias=False, w_init=hk.initializers.UniformScaling(a)),
        ]
        self._vel_correction_mlp = hk.Sequential(net)

        # attention
        self._attention_mlp = None
        if attention:
            self._attention_mlp = hk.Sequential(
                [LinearXav(hidden_size), jax.nn.sigmoid]
            )

    def _pos_update(
        self,
        pos: jnp.ndarray,
        graph: jraph.GraphsTuple,
        coord_diff: jnp.ndarray,
    ) -> jnp.ndarray:
        trans = coord_diff * self._pos_correction_mlp(graph.edges)
        # NOTE: was in the original code
        trans = jnp.clip(trans, -100, 100)
        return self.pos_aggregate_fn(trans, graph.senders, num_segments=pos.shape[0])

    def _message(
        self,
        radial: jnp.ndarray,
        edge_attribute: jnp.ndarray,
        edge_features: Any,
        incoming: jnp.ndarray,
        outgoing: jnp.ndarray,
        globals_: Any,
    ) -> jnp.ndarray:
        _ = edge_features
        _ = globals_
        msg = jnp.concatenate([incoming, outgoing, radial], axis=-1)
        if edge_attribute is not None:
            msg = jnp.concatenate([msg, edge_attribute], axis=-1)
        msg = self._edge_mlp(msg)
        if self._attention_mlp:
            att = self._attention_mlp(msg)
            msg = msg * att
        return msg

    def _update(
        self,
        node_attribute: jnp.ndarray,
        nodes: jnp.ndarray,
        senders: Any,
        msg: jnp.ndarray,
        globals_: Any,
    ) -> jnp.ndarray:
        _ = senders
        _ = globals_
        x = jnp.concatenate([nodes, msg], axis=-1)
        if node_attribute is not None:
            x = jnp.concatenate([x, node_attribute], axis=-1)
        x = self._node_mlp(x)
        if self._residual:
            x = nodes + x
        return x

    def _coord2radial(
        self, graph: jraph.GraphsTuple, coord: jnp.array
    ) -> Tuple[jnp.array, jnp.array]:
        coord_diff = coord[graph.senders] - coord[graph.receivers]
        radial = jnp.sum(coord_diff**2, 1)[:, jnp.newaxis]
        if self._normalize:
            norm = jnp.sqrt(radial)
            coord_diff = coord_diff / (norm + self._eps)
        return radial, coord_diff

    def __call__(
        self,
        graph: jraph.GraphsTuple,
        pos: jnp.ndarray,
        vel: jnp.ndarray,
        edge_attribute: Optional[jnp.ndarray] = None,
        node_attribute: Optional[jnp.ndarray] = None,
    ) -> Tuple[jraph.GraphsTuple, jnp.ndarray]:
        """
        Apply EGNN layer.

        Args:
            graph: Graph from previous step
            pos: Node position, updated separately
            vel: Node velocity
            edge_attribute: Edge attribute (optional)
            node_attribute: Node attribute (optional)
        Returns:
            Updated graph, node position
        """
        radial, coord_diff = self._coord2radial(graph, pos)

        graph = jraph.GraphNetwork(
            update_edge_fn=Partial(self._message, radial, edge_attribute),
            update_node_fn=Partial(self._update, node_attribute),
            aggregate_edges_for_nodes_fn=self.msg_aggregate_fn,
        )(graph)
        # update position
        pos = pos + self._pos_update(pos, graph, coord_diff)
        # integrate velocity
        shift = self._vel_correction_mlp(graph.nodes) * vel
        pos = pos + jnp.clip(shift, -100, 100)
        return graph, pos


class EGNN(BaseModel):
    r"""
    E(n) Graph Neural Network (https://arxiv.org/abs/2102.09844).

    Original implementation: https://github.com/vgsatorras/egnn
    """

    def __init__(
        self,
        hidden_size: int,
        output_size: int,
        n_vels: int,
        displacement_fn: space.DisplacementFn,
        act_fn: Callable = jax.nn.silu,
        num_layers: int = 4,
        velocity_aggregate: str = "avg",
        homogeneous_particles: bool = True,
        residual: bool = True,
        attention: bool = False,
        normalize: bool = False,
        tanh: bool = False,
    ):
        r"""
        Initialize the network.

        Args:
            hidden_size: Number of hidden features
            output_size: Number of features for 'h' at the output
            n_vels: Number of velocities in the history.
            displacement_fn: Displacement function for the acceleration computation.
            act_fn: Non-linearity
            num_layers: Number of layer for the EGNN
            velocity_aggregate: Velocity sequence aggregation method.
            homogeneous_particles: If all particles are of homogeneous type.
            residual: Use residual connections, we recommend not changing this one
            attention: Whether using attention or not
            normalize: Normalizes the coordinates messages such that:
                x^{l+1}_i = x^{l}_i + \sum(x_i - x_j)\phi_x(m_{ij})\|x_i - x_j\|
                It may help in the stability or generalization. Not used in the paper.
            tanh: Sets a tanh activation function at the output of \phi_x(m_{ij}). It
                bounds the output of \phi_x(m_{ij}) which definitely improves in
                stability but it may decrease in accuracy. Not used in the paper.
        """
        super().__init__()
        self._hidden_size = hidden_size
        self._output_size = output_size
        self._displacement_fn = displacement_fn
        self._act_fn = act_fn
        self._num_layers = num_layers
        self._residual = residual
        self._attention = attention
        self._normalize = normalize
        self._tanh = tanh
        # transform
        assert velocity_aggregate in [
            "avg",
            "sum",
            "last",
        ], "Invalid velocity aggregate. Must be one of 'avg', 'sum' or 'last'."
        self._velocity_aggregate = velocity_aggregate
        self._n_vels = n_vels
        self._homogeneous_particles = homogeneous_particles

    def _transform(
        self, features: Dict[str, jnp.ndarray], particle_type: jnp.ndarray
    ) -> Tuple[jraph.GraphsTuple, Dict[str, jnp.ndarray]]:
        props = {}
        n_nodes = features["vel_hist"].shape[0]

        traj = jnp.reshape(features["vel_hist"], (n_nodes, self._n_vels, -1))

        if self._n_vels == 1:
            props["vel"] = jnp.squeeze(traj)
        else:
            if self._velocity_aggregate == "avg":
                props["vel"] = jnp.mean(traj, 1)
            if self._velocity_aggregate == "sum":
                props["vel"] = jnp.sum(traj, 1)
            if self._velocity_aggregate == "last":
                props["vel"] = traj[:, -1, :]

        # most recent position
        props["pos"] = features["abs_pos"][:, -1]
        # relative distances between particles
        props["edge_attr"] = features["rel_dist"]
        # force magnitude as node attributes
        props["node_attr"] = jnp.sum(features["force"] ** 2, -1, keepdims=True)

        # velocity magnitudes as node features
        node_features = jnp.concatenate(
            [
                jnp.linalg.norm(traj[:, i, :], axis=-1, keepdims=True)
                for i in range(self._n_vels)
            ],
            axis=-1,
        )
        if not self._homogeneous_particles:
            particles = jax.nn.one_hot(particle_type, NodeType.SIZE)
            node_features = jnp.concatenate([node_features, particles], axis=-1)

        graph = jraph.GraphsTuple(
            nodes=node_features,
            edges=None,
            senders=features["senders"],
            receivers=features["receivers"],
            n_node=jnp.array([n_nodes]),
            n_edge=jnp.array([len(features["senders"])]),
            globals=None,
        )

        return graph, props

    def _postprocess(
        self, next_pos: jnp.ndarray, props: Dict[str, jnp.ndarray]
    ) -> jnp.ndarray:
        prev_vel = props["vel"]
        prev_pos = props["pos"]
        # first order finite difference
        next_vel = self._displacement_fn(next_pos, prev_pos)
        acc = next_vel - prev_vel
        return acc

    def __call__(
        self, sample: Tuple[Dict[str, jnp.ndarray], jnp.ndarray]
    ) -> jnp.ndarray:
        graph, props = self._transform(*sample)
        # input node embedding
        h = LinearXav(self._hidden_size, name="embedding")(graph.nodes)
        graph = graph._replace(nodes=h)
        # message passing
        next_pos = props["pos"].copy()
        for n in range(self._num_layers):
            graph, next_pos = EGNNLayer(
                layer_num=n,
                hidden_size=self._hidden_size,
                output_size=self._hidden_size,
                act_fn=self._act_fn,
                residual=self._residual,
                attention=self._attention,
                normalize=self._normalize,
                tanh=self._tanh,
            )(graph, next_pos, props["vel"], props["edge_attr"], props["node_attr"])

        # position finite differencing to get acceleration
        out = self._postprocess(next_pos, props)
        return out

    @classmethod
    def setup_model(cls, args: Namespace) -> Tuple["EGNN", Type]:
        dtype = jnp.float64 if args.config.f64 else jnp.float32

        def displacement_fn(x, y):
            return jax.lax.cond(
                jnp.array(args.metadata["periodic_boundary_conditions"]).any(),
                lambda x, y: space.periodic(jnp.array(args.box))[0](x, y).astype(dtype),
                lambda x, y: space.free()[0](x, y).astype(dtype),
                x,
                y,
            )

        displacement_fn = jax.vmap(displacement_fn, in_axes=(0, 0))
        return cls(
            hidden_size=args.config.latent_dim,
            output_size=1,
            displacement_fn=displacement_fn,
            num_layers=args.config.num_mp_steps,
            velocity_aggregate=args.config.velocity_aggregate,
            n_vels=args.config.input_seq_length - 1,
            residual=True,
        )
