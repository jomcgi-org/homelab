#!/usr/bin/env python3
"""Generate a SigNoz RED dashboard JSON for the monolith service.

Produces a dashboard with Rate, Errors, and Duration panels filtered by a
sub-service template variable.  The variable is a CUSTOM dropdown whose
values are the known route prefixes (e.g. /api/home, /api/notes).

Usage:
    python3 generate-red-dashboard.py > ../chart/dashboards/monolith-red.json
"""

import json
import uuid

# Known sub-services and their route prefixes.
# Add new entries here when a new router is added to main.py.
SUB_SERVICES = {
    "home": "/api/home",
    "notes": "/api/notes",
    "chat": "/api/chat",
}

SERVICE_NAME = "monolith-backend"


def _id():
    return str(uuid.uuid4())


def _service_filter():
    """Filter: serviceName = monolith-backend."""
    return {
        "key": {
            "key": "serviceName",
            "dataType": "string",
            "type": "tag",
            "isColumn": True,
            "isJSON": False,
        },
        "op": "=",
        "value": SERVICE_NAME,
    }


def _sub_service_filter():
    """Filter: operation name contains the selected sub-service prefix."""
    return {
        "key": {
            "key": "name",
            "dataType": "string",
            "type": "tag",
            "isColumn": True,
            "isJSON": False,
        },
        "op": "contains",
        "value": "{{.sub_service}}",
    }


def _error_filter():
    """Filter: hasError = true."""
    return {
        "key": {
            "key": "hasError",
            "dataType": "bool",
            "type": "tag",
            "isColumn": True,
            "isJSON": False,
        },
        "op": "=",
        "value": "true",
    }


def _base_query(
    name, agg_op, agg_attr=None, extra_filters=None, group_by=None, legend=""
):
    """Build a single query builder entry for traces."""
    filters = [_service_filter(), _sub_service_filter()]
    if extra_filters:
        filters.extend(extra_filters)

    q = {
        "queryName": name,
        "stepInterval": 60,
        "dataSource": "traces",
        "aggregateOperator": agg_op,
        "aggregateAttribute": agg_attr or {},
        "groupBy": group_by or [],
        "expression": name,
        "filters": {"items": filters, "op": "AND"},
        "having": {"expression": ""},
        "legend": legend,
        "limit": None,
        "orderBy": [],
        "selectColumns": [],
        "functions": [],
        "aggregations": [],
        "disabled": False,
        "reduceTo": "avg",
    }
    return q


def _query_wrapper(query_data, query_formulas=None):
    """Wrap query data in the full query object."""
    return {
        "queryType": "builder",
        "promql": [{"disabled": False, "legend": "", "name": "A", "query": ""}],
        "clickhouse_sql": [{"disabled": False, "legend": "", "name": "A", "query": ""}],
        "builder": {
            "queryData": query_data,
            "queryFormulas": query_formulas or [],
        },
    }


def _widget(
    widget_id, title, description, query, panel_type="graph", y_unit=None, stacked=False
):
    """Build a complete widget definition."""
    w = {
        "id": widget_id,
        "panelTypes": panel_type,
        "title": title,
        "description": description,
        "fillSpans": False,
        "isStacked": stacked,
        "nullZeroValues": "zero",
        "opacity": "1",
        "softMax": None,
        "softMin": None,
        "selectedLogFields": [],
        "selectedTracesFields": [],
        "thresholds": [],
        "contextLinks": {"linksData": []},
        "query": query,
    }
    if y_unit:
        w["yAxisUnit"] = y_unit
    return w


def _operation_group_by():
    return [
        {
            "key": "name",
            "dataType": "string",
            "type": "tag",
            "isColumn": True,
            "isJSON": False,
        }
    ]


def build_dashboard():
    # Widget IDs (stable so sidecar updates rather than recreates).
    rate_id = "red-request-rate"
    error_id = "red-error-count"
    latency_id = "red-latency"
    error_pct_id = "red-error-pct"

    # --- Request Rate panel ---
    rate_widget = _widget(
        rate_id,
        "Request Rate (ops/s)",
        "Number of requests per second by operation",
        _query_wrapper(
            [
                _base_query(
                    "A", "rate", group_by=_operation_group_by(), legend="{{name}}"
                ),
            ]
        ),
    )

    # --- Error Count panel ---
    error_widget = _widget(
        error_id,
        "Errors",
        "Error count by operation",
        _query_wrapper(
            [
                _base_query(
                    "A",
                    "count",
                    extra_filters=[_error_filter()],
                    group_by=_operation_group_by(),
                    legend="{{name}}",
                ),
            ]
        ),
        stacked=True,
    )

    # --- Latency panel (p50 / p95 / p99) ---
    duration_attr = {
        "key": "durationNano",
        "dataType": "float64",
        "type": "tag",
        "isColumn": True,
        "isJSON": False,
    }
    latency_widget = _widget(
        latency_id,
        "Latency (p50 / p95 / p99)",
        "Request duration percentiles",
        _query_wrapper(
            [
                _base_query("A", "p50", agg_attr=duration_attr, legend="p50"),
                _base_query("B", "p95", agg_attr=duration_attr, legend="p95"),
                _base_query("C", "p99", agg_attr=duration_attr, legend="p99"),
            ]
        ),
        y_unit="ns",
    )

    # --- Error Rate % panel ---
    error_pct_widget = _widget(
        error_pct_id,
        "Error Rate (%)",
        "Percentage of requests that resulted in errors",
        _query_wrapper(
            [
                _base_query(
                    "A", "count", extra_filters=[_error_filter()], legend="errors"
                ),
                _base_query("B", "count", legend="total"),
            ],
            query_formulas=[
                {
                    "queryName": "F1",
                    "expression": "A*100/B",
                    "disabled": False,
                    "legend": "error %",
                    "stepInterval": 60,
                    "dataSource": "traces",
                    "groupBy": [],
                    "having": {"expression": ""},
                    "limit": None,
                    "orderBy": [],
                    "selectColumns": [],
                    "functions": [],
                    "aggregations": [],
                }
            ],
        ),
    )

    # --- Sub-service variable ---
    var_id = _id()
    custom_values = ",".join(SUB_SERVICES.values())
    variables = {
        var_id: {
            "id": var_id,
            "name": "sub_service",
            "description": "Filter by sub-service route prefix",
            "type": "CUSTOM",
            "customValue": custom_values,
            "sort": "ASC",
            "multiSelect": False,
            "showALLOption": True,
            "allSelected": True,
            "selectedValue": "ALL",
            "order": 0,
        }
    }

    # --- Layout (2x2 grid, 12-column) ---
    layout = [
        {"h": 3, "i": rate_id, "w": 6, "x": 0, "y": 0, "moved": False, "static": False},
        {
            "h": 3,
            "i": error_id,
            "w": 6,
            "x": 6,
            "y": 0,
            "moved": False,
            "static": False,
        },
        {
            "h": 3,
            "i": latency_id,
            "w": 6,
            "x": 0,
            "y": 3,
            "moved": False,
            "static": False,
        },
        {
            "h": 3,
            "i": error_pct_id,
            "w": 6,
            "x": 6,
            "y": 3,
            "moved": False,
            "static": False,
        },
    ]

    dashboard = {
        "title": "Monolith RED Dashboard",
        "description": (
            "Rate, Errors, and Duration for the monolith backend. "
            "Use the sub_service variable to filter by route prefix "
            f"({', '.join(SUB_SERVICES.keys())})."
        ),
        "tags": ["monolith", "red", "performance", "traces"],
        "variables": variables,
        "version": "v5",
        "layout": layout,
        "widgets": [rate_widget, error_widget, latency_widget, error_pct_widget],
    }

    return dashboard


if __name__ == "__main__":
    print(json.dumps(build_dashboard(), indent=2))
