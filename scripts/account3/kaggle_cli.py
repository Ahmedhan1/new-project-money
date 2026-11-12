"""Run the official Kaggle CLI in this cloud session, where the API token is injected by the network proxy.

The CLI validates a token by sending it in a request BODY (introspection), which a header-injecting proxy cannot fill,
so this wrapper answers the introspection locally with the account name and lets every real API call go out with a
placeholder token that the proxy replaces in the Authorization header. On a normal machine use `kaggle` directly.

    python scripts/account3/kaggle_cli.py kernels status <user>/<slug>
"""
import os
import sys

USER = os.environ.get("NEXUS_KAGGLE_USER", "ahmedhanimohamed")
os.environ.setdefault("KAGGLE_API_TOKEN", "KGAT" + "_" + "0" * 32)  # dummy: the proxy injects the real one

from kaggle.api.kaggle_api_extended import KaggleApi  # noqa: E402

KaggleApi._introspect_token = lambda self, token: USER

if __name__ == "__main__":
    from kaggle.cli import main
    sys.argv = ["kaggle", *sys.argv[1:]]
    sys.exit(main())
