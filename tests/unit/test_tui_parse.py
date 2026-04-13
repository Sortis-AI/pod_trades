"""Smoke tests for TUI module imports and simple widget construction.

We don't try to instantiate the full Textual App here (it wants a real
terminal and an event loop). Instead we import every TUI module to catch
import-time errors (missing imports, syntax errors, bad type annotations).
"""


def test_tui_module_imports() -> None:
    import pod_the_trader.tui
    import pod_the_trader.tui.app
    import pod_the_trader.tui.publisher
    import pod_the_trader.tui.widgets
    import pod_the_trader.tui.widgets.cycle_status
    import pod_the_trader.tui.widgets.health
    import pod_the_trader.tui.widgets.ledger
    import pod_the_trader.tui.widgets.level5
    import pod_the_trader.tui.widgets.log_tail
    import pod_the_trader.tui.widgets.pod_logo
    import pod_the_trader.tui.widgets.portfolio
    import pod_the_trader.tui.widgets.prices

    # Just ensure the modules are real.
    assert pod_the_trader.tui.app.PodDashboardApp is not None


def test_tui_app_is_publisher() -> None:
    """PodDashboardApp should structurally implement Publisher.

    We check by looking at the methods on the class, not by runtime
    isinstance (constructing a Textual App has side effects).
    """
    from pod_the_trader.tui.app import PodDashboardApp
    from pod_the_trader.tui.publisher import Publisher

    required = [
        name for name in dir(Publisher) if name.startswith("on_")
    ]
    for method_name in required:
        assert hasattr(PodDashboardApp, method_name), (
            f"PodDashboardApp missing {method_name}"
        )
