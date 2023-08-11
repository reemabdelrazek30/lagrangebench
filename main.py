import os
from argparse import Namespace

import yaml

from experiments.config import NestedLoader, cli_arguments

if __name__ == "__main__":
    cli_args = cli_arguments()
    if "config" in cli_args:
        config_path = cli_args["config"]
    elif "model_dir" in cli_args:
        config_path = os.path.join(cli_args["model_dir"], "config.yaml")

    with open(config_path, "r") as f:
        args = yaml.load(f, NestedLoader)

    # priority to command line arguments
    args.update(cli_args)
    args = Namespace(config=Namespace(**args), info=Namespace())

    # specify cuda device
    os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"  # see issue #152
    os.environ["CUDA_VISIBLE_DEVICES"] = str(args.config.gpu)

    if args.config.f64:
        from jax.config import config

        config.update("jax_enable_x64", True)

    from experiments.run import train_or_infer

    train_or_infer(args)
