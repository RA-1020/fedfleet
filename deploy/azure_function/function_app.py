"""HTTP-triggered round report. GET /api/report?code=<function key>[&run=<name>][&format=json|html]."""
import json
import logging
import os

import azure.functions as func

from fedfleet_report import build_report, render_html

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)


@app.route(route="report", methods=["GET"])
def report(req: func.HttpRequest) -> func.HttpResponse:
    sas = os.environ.get("BLOB_SAS_URL", "")
    if not sas:
        return func.HttpResponse("BLOB_SAS_URL is not set", status_code=500)
    try:
        data = build_report(sas, run=req.params.get("run"))
    except ValueError as e:
        return func.HttpResponse(str(e), status_code=500)
    except Exception as e:                        # never echo the exception text: it may carry the SAS URL
        logging.error("report failed: %s", type(e).__name__)
        return func.HttpResponse(f"report failed: {type(e).__name__}", status_code=502)
    if req.params.get("format", "html") == "json":
        return func.HttpResponse(json.dumps(data, indent=2), mimetype="application/json")
    return func.HttpResponse(render_html(data), mimetype="text/html")
