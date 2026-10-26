WIDGET_JS_TEMPLATE = """(function() {{
    var widgetId = "{widget_id}";
    var script = document.currentScript;
    var apiBase = (script && script.getAttribute && script.getAttribute("data-api-base")) || "";
    if (!apiBase && script && script.src) {{
        var marker = "/public/widget/";
        var idx = script.src.indexOf(marker);
        if (idx !== -1) {{
            apiBase = script.src.substring(0, idx);
        }}
    }}
    if (!apiBase) {{
        apiBase = window.location.origin;
    }}
    var configUrl = apiBase + "/public/widget/" + widgetId + "/config";

    var xhr = new XMLHttpRequest();
    xhr.open("GET", configUrl, true);
    xhr.onload = function() {{
        if (xhr.status !== 200) return;
        var config = JSON.parse(xhr.responseText);

        var container = document.createElement("div");
        container.id = "fr-widget-" + widgetId;
        container.style.cssText = "all:initial;position:fixed;bottom:20px;right:20px;z-index:999999;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;";

        var btn = document.createElement("button");
        btn.textContent = config.button_text || "Get a Quote";
        btn.style.cssText = "background:" + (config.brand_color || "#2563eb") + ";color:#fff;border:none;padding:14px 28px;border-radius:8px;cursor:pointer;font-size:16px;font-weight:600;box-shadow:0 4px 12px rgba(0,0,0,0.15);";
        container.appendChild(btn);

        var modal = document.createElement("div");
        modal.style.cssText = "display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);z-index:1000000;align-items:center;justify-content:center;";
        modal.id = "fr-modal-" + widgetId;

        var formContainer = document.createElement("div");
        formContainer.style.cssText = "background:#fff;border-radius:12px;padding:32px;max-width:400px;width:90%;box-shadow:0 8px 32px rgba(0,0,0,0.2);position:relative;";

        var closeBtn = document.createElement("button");
        closeBtn.innerHTML = "&times;";
        closeBtn.style.cssText = "position:absolute;top:8px;right:12px;border:none;background:none;font-size:24px;cursor:pointer;color:#666;";
        formContainer.appendChild(closeBtn);

        var form = document.createElement("form");
        form.id = "fr-form-" + widgetId;
        form.style.cssText = "display:flex;flex-direction:column;gap:12px;";

        var heading = document.createElement("h3");
        heading.textContent = config.button_text || "Get a Quote";
        heading.style.cssText = "margin:0 0 8px;font-size:20px;color:#333;font-weight:600;";
        form.appendChild(heading);

        (config.fields || ["name", "email", "phone"]).forEach(function(field) {{
            var label = document.createElement("label");
            label.textContent = field.charAt(0).toUpperCase() + field.slice(1);
            label.style.cssText = "font-size:14px;color:#555;font-weight:500;";
            var input = document.createElement("input");
            input.name = field;
            input.placeholder = "Enter your " + field;
            input.style.cssText = "width:100%;padding:10px 12px;border:1px solid #ddd;border-radius:6px;font-size:14px;box-sizing:border-box;outline:none;";
            input.onfocus = function() {{ this.style.borderColor = config.brand_color || "#2563eb"; }};
            input.onblur = function() {{ this.style.borderColor = "#ddd"; }};
            label.appendChild(input);
            form.appendChild(label);
        }});

        var honeypot = document.createElement("input");
        honeypot.type = "text";
        honeypot.name = config.honeypot_field || "_hp_a3f9";
        honeypot.style.cssText = "position:absolute;left:-9999px;top:-9999px;opacity:0;height:0;width:0;";
        honeypot.tabIndex = -1;
        honeypot.autocomplete = "off";
        form.appendChild(honeypot);

        var submitBtn = document.createElement("button");
        submitBtn.type = "submit";
        submitBtn.textContent = "Submit";
        submitBtn.style.cssText = "background:" + (config.brand_color || "#2563eb") + ";color:#fff;border:none;padding:12px;border-radius:6px;cursor:pointer;font-size:16px;font-weight:600;margin-top:4px;";
        form.appendChild(submitBtn);

        var message = document.createElement("div");
        message.id = "fr-msg-" + widgetId;
        message.style.cssText = "display:none;text-align:center;padding:12px;border-radius:6px;font-size:14px;";
        form.appendChild(message);

        form.onsubmit = function(e) {{
            e.preventDefault();
            var fd = new FormData(form);
            var formData = {{}};
            fd.forEach(function(value, key) {{ formData[key] = value; }});
            var payload = JSON.stringify({{ form_data: formData, referer: window.location.href }});
            submitBtn.disabled = true;
            submitBtn.textContent = "Sending...";
            var postXhr = new XMLHttpRequest();
            postXhr.open("POST", apiBase + "/public/widget/" + widgetId + "/submit", true);
            postXhr.setRequestHeader("Content-Type", "application/json");
            postXhr.onload = function() {{
                submitBtn.disabled = false;
                submitBtn.textContent = "Submit";
                if (postXhr.status === 201 || postXhr.status === 200) {{
                    var msgDiv = document.getElementById("fr-msg-" + widgetId);
                    msgDiv.style.cssText = "display:block;text-align:center;padding:12px;border-radius:6px;font-size:14px;background:#e8f5e9;color:#2e7d32;";
                    msgDiv.textContent = config.success_message || "Thanks!";
                    form.querySelectorAll("input").forEach(function(inp) {{ if (inp.type !== "hidden") inp.value = ""; }});
                    setTimeout(function() {{ msgDiv.style.display = "none"; }}, 5000);
                }} else {{
                    var msgDiv = document.getElementById("fr-msg-" + widgetId);
                    msgDiv.style.cssText = "display:block;text-align:center;padding:12px;border-radius:6px;font-size:14px;background:#ffebee;color:#c62828;";
                    msgDiv.textContent = "Something went wrong. Please try again.";
                }}
            }};
            postXhr.send(payload);
        }};

        formContainer.appendChild(form);
        modal.appendChild(formContainer);
        container.appendChild(modal);
        document.body.appendChild(container);

        btn.onclick = function() {{
            modal.style.display = "flex";
        }};
        closeBtn.onclick = function() {{
            modal.style.display = "none";
        }};
        modal.onclick = function(e) {{
            if (e.target === modal) modal.style.display = "none";
        }};
    }};
    xhr.send();
}})();
"""


DEFAULT_API_BASE_URL = "http://localhost:8000"


def render_widget_js(widget_id: str, config: dict, js_version: int) -> str:
    return WIDGET_JS_TEMPLATE.format(widget_id=widget_id)


def generate_script_tag(
    widget_id: str, js_version: int, base_url: str = DEFAULT_API_BASE_URL
) -> str:
    src = f"{base_url.rstrip('/')}/public/widget/{widget_id}/widget.js?v={js_version}"
    return f'<script src="{src}" data-widget-id="{widget_id}" defer></script>'
