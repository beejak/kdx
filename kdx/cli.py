from pathlib import Path

import click

from kdx import __version__
from kdx.collector.types import DiagnosisError


@click.group()
@click.version_option(__version__, prog_name="kdx")
def cli():
    pass


@cli.command("diagnose")
@click.argument("deployment")
@click.option("-n", "--namespace", default="default", show_default=True)
@click.option("--mock", "mock_fixture", default=None)
@click.option("--dump-context", "dump_context", default=None)
@click.option("--context", "kube_context", default=None)
def diagnose(
    deployment: str,
    namespace: str,
    mock_fixture: str | None,
    dump_context: str | None,
    kube_context: str | None,
):
    from kdx.config import Settings
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
        from kdx.diagnosis.engine import diagnose as run_diagnosis

        result = run_diagnosis(ctx, settings)
        print_result(ctx, result)
    except DiagnosisError as exc:
        print_error(str(exc))
        raise SystemExit(1) from exc
    except SystemExit:
        raise
    except Exception as exc:
        print_error(str(exc))
        raise SystemExit(1) from exc
