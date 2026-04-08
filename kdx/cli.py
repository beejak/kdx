from pathlib import Path

import click
from dotenv import load_dotenv

from kdx import __version__
from kdx.collector.types import DiagnosisError

load_dotenv()


@click.group()
@click.version_option(__version__, prog_name="kdx")
def cli():
    """Diagnose failing Kubernetes Deployments from collected signals and an LLM."""


@cli.command(
    "diagnose",
    short_help="Collect signals for DEPLOYMENT and print a diagnosis.",
)
@click.argument("deployment")
@click.option(
    "-n",
    "--namespace",
    default="default",
    show_default=True,
    help="Kubernetes namespace for the Deployment.",
)
@click.option(
    "--mock",
    "mock_fixture",
    metavar="FIXTURE",
    default=None,
    help="Use a built-in fixture instead of the cluster (e.g. crash_loop, oom_kill).",
)
@click.option(
    "--dump-context",
    "dump_context",
    metavar="PATH",
    default=None,
    help="Write DiagnosisContext JSON to PATH before calling the model.",
)
@click.option(
    "--context",
    "kube_context",
    metavar="NAME",
    default=None,
    help="Kubeconfig context name (default: current context).",
)
def diagnose(
    deployment: str,
    namespace: str,
    mock_fixture: str | None,
    dump_context: str | None,
    kube_context: str | None,
):
    """Inspect DEPLOYMENT, send a structured snapshot to the model, print the diagnosis.

    With --mock FIXTURE, context is loaded from tests/fixtures/FIXTURE.json (stem only).
    The DEPLOYMENT argument is still required but not used for collection in mock mode.
    """
    from kdx.config import Settings, build_provider
    from kdx.diagnosis.engine import diagnose as run_diagnosis
    from kdx.output.formatter import print_error, print_result

    try:
        if mock_fixture:
            from kdx.collector.mock import load_fixture

            try:
                ctx = load_fixture(mock_fixture)
            except FileNotFoundError as exc:
                print_error(str(exc))
                raise SystemExit(2) from exc
        else:
            from kdx.collector.k8s import collect

            ctx = collect(deployment, namespace, kube_context)
        if dump_context:
            Path(dump_context).write_text(ctx.model_dump_json(indent=2))
        settings = Settings()
        provider = build_provider(settings)
        result = run_diagnosis(ctx, provider, settings.max_tokens)
        print_result(ctx, result)
    except DiagnosisError as exc:
        print_error(str(exc))
        raise SystemExit(1) from exc
    except SystemExit:
        raise
    except Exception as exc:
        print_error(str(exc))
        raise SystemExit(1) from exc
