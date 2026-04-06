from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

from kdx.collector.types import DiagnosisContext, DiagnosisResult

_console = Console()


def print_error(message: str) -> None:
    _console.print(Panel(message, title="[red]Error[/red]", border_style="red"))


def print_result(ctx: DiagnosisContext, result: DiagnosisResult) -> None:
    _console.print(
        Panel.fit(
            f"[bold]{ctx.deployment.name}[/bold] / {ctx.namespace}\n"
            f"Cluster: {ctx.cluster_name}  ·  Pre-class: {ctx.failure_class}",
            title="Context",
        )
    )
    _console.print(
        Panel.fit(
            f"[bold]{result.failure_class}[/bold]  ({result.confidence})\n\n{result.root_cause}",
            title="Diagnosis",
        )
    )
    for line in result.evidence:
        _console.print(f"  • {line}")
    _console.print(
        Panel(Syntax(result.fix_command, "text", theme="ansi_dark"), title="fix_command")
    )
    _console.print(Panel(result.fix_explanation, title="fix_explanation"))
