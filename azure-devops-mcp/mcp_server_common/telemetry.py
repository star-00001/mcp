"""Optional OpenTelemetry export to Azure Monitor / Application Insights.

Enabled when ``APPLICATIONINSIGHTS_CONNECTION_STRING`` is set and the ``azure`` extra
(``azure-monitor-opentelemetry``) is installed. Otherwise this is a no-op, so local runs and
tests need nothing. Logs never include tokens or tool arguments (see audit design).
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)


def setup_telemetry(service_name: str, *, fastapi_app: Any = None) -> bool:
    if not os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        return False
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
    except ImportError:
        log.warning(
            "APPLICATIONINSIGHTS_CONNECTION_STRING is set but the 'azure' extra "
            "is not installed; telemetry disabled"
        )
        return False
    os.environ.setdefault("OTEL_SERVICE_NAME", service_name)
    configure_azure_monitor(logger_name="", enable_live_metrics=False)
    if fastapi_app is not None:
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            FastAPIInstrumentor.instrument_app(fastapi_app, excluded_urls="healthz")
        except ImportError:
            pass
    log.info("telemetry: exporting to Azure Monitor as %s", service_name)
    return True
