"""Static POD ASCII logo panel."""

from textual.widgets import Static

LOGO = r"""
[b #ffcc00]██████   ██████  ██████[/]
[b #ffcc00]██   ██ ██    ██ ██   ██[/]
[b #ffcc00]██████  ██    ██ ██   ██[/]
[b #ffcc00]██      ██    ██ ██   ██[/]
[b #ffcc00]██       ██████  ██████[/]

[dim]the trader[/]
"""


class PodLogo(Static):
    """Static ASCII logo for the Pod The Trader brand."""

    DEFAULT_CSS = """
    PodLogo {
        content-align: center middle;
        width: 1fr;
        height: 1fr;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(LOGO, **kwargs)
