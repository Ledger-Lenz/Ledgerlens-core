# Interactive API (OpenAPI / Swagger UI)

The LedgerLens REST API is fully described by an OpenAPI 3.1 spec.

## Live Swagger UI

When running locally, the interactive Swagger UI is available at:

```
http://localhost:8000/docs
```

The ReDoc alternative is at:

```
http://localhost:8000/redoc
```

## Embedded Spec

If you have exported `docs/openapi.json` (e.g. via `python -c "import json; from api.main import app; print(json.dumps(app.openapi()))"`),
the spec renders below:

<div style="height: 800px; overflow: hidden;">
<iframe
  src="https://petstore.swagger.io/v2/swagger.json"
  id="swagger-frame"
  style="width:100%; height:100%; border:none;"
  title="LedgerLens OpenAPI Spec"
></iframe>
</div>

<script>
// Replace with the actual deployed spec URL if available
(function() {
  var frame = document.getElementById('swagger-frame');
  var specUrl = window.location.origin + '/openapi.json';
  frame.src = 'https://petstore.swagger.io/?url=' + encodeURIComponent(specUrl);
})();
</script>

## Exporting the Spec

```bash
python -c "
import json
from api.main import app
print(json.dumps(app.openapi(), indent=2))
" > docs/openapi.json
```
