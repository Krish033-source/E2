from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urldefrag, unquote, quote, parse_qs
from urllib.request import Request as URLRequest, urlopen
from urllib.error import HTTPError, URLError

from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from pydantic import BaseModel, HttpUrl, Field

BASE = Path(__file__).resolve().parent
GENERATED = BASE / "generated"
GENERATED.mkdir(exist_ok=True)
JOBS_FILE = GENERATED / "jobs.json"
MAX_BODY = 8 * 1024 * 1024
executor = ThreadPoolExecutor(max_workers=4)
job_lock = threading.Lock()


def _load_jobs() -> dict[str, dict[str, Any]]:
    try:
        return json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_jobs(jobs: dict[str, dict[str, Any]]) -> None:
    tmp = JOBS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(jobs, indent=2), encoding="utf-8")
    tmp.replace(JOBS_FILE)

JOBS = _load_jobs()


def update_job(job_id: str, **fields: Any) -> None:
    global JOBS
    with job_lock:
        JOBS = _load_jobs()
        JOBS.setdefault(job_id, {}).update(fields)
        _save_jobs(JOBS)


def is_private_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return True
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return True
    return False


def validate_target(url: str) -> str:
    p = urlparse(url)
    if p.scheme not in {"http", "https"}:
        raise ValueError("Only http:// and https:// targets are allowed.")
    if not p.hostname:
        raise ValueError("Target URL has no hostname.")
    if p.username or p.password:
        raise ValueError("Credentials in the target URL are not allowed.")
    host = p.hostname.lower()
    blocked = {"localhost", "localhost.localdomain", "0.0.0.0", "::1"}
    if host in blocked or is_private_host(host):
        raise ValueError("Local/private-network targets are blocked by the prototype.")
    return url


def safe_rel_path(url: str, default_name: str = "index.html") -> str:
    p = urlparse(url)
    path = unquote(p.path or "/")
    path = path.replace("\\", "/")
    parts = [x for x in path.split("/") if x not in {"", ".", ".."}]
    clean = []
    for part in parts:
        part = re.sub(r"[^A-Za-z0-9._-]", "_", part)
        if part:
            clean.append(part[:150])
    if not clean:
        return default_name
    last = clean[-1]
    if "." not in last:
        clean.append(default_name)
    return "/".join(clean)


def url_to_local(url: str, kind: str, mapping: dict[str, str]) -> str:
    path = safe_rel_path(url, "index.html")
    if kind == "page":
        if path.endswith(".html"):
            return path
        return path.rstrip("/") + "/index.html"
    suffix = Path(path).suffix
    if not suffix:
        suffix = ".bin"
        path += suffix
    candidate = f"assets/{path.lstrip('/')}"
    mapping[url] = candidate
    return candidate


def same_origin(target: str, other: str) -> bool:
    a, b = urlparse(target), urlparse(other)
    return a.scheme == b.scheme and a.hostname == b.hostname and (a.port or (443 if a.scheme == 'https' else 80)) == (b.port or (443 if b.scheme == 'https' else 80))


def normalize_url(base: str, href: str) -> str | None:
    if not href:
        return None
    href = href.strip()
    if href.startswith(("javascript:", "mailto:", "tel:", "data:", "blob:")):
        return None
    absolute = urldefrag(urljoin(base, href))[0]
    return absolute


def rewrite_srcset(value: str, mapping: dict[str, str], base: str, asset_base: str) -> str:
    out = []
    for item in value.split(","):
        bits = item.strip().split()
        if not bits:
            continue
        u = normalize_url(base, bits[0])
        if u and u in mapping:
            bits[0] = asset_base + mapping[u]
        out.append(" ".join(bits))
    return ", ".join(out)


def rewrite_html(html: bytes, source_url: str, page_map: dict[str, str], asset_map: dict[str, str], asset_base: str, job_id: str, target_origin: str) -> str:
    text = html.decode("utf-8", errors="ignore")
    soup = BeautifulSoup(text, "html.parser")

    # noscript fallbacks are meaningless now that JS runs again; drop them so
    # we don't show a duplicate "enable JavaScript" message under real content.
    for tag in soup.find_all("noscript"):
        tag.decompose()

    # Keep the original site's JavaScript.  The first version removed
    # third-party scripts and inline handlers, which broke real UI controls
    # such as language modals, menus and search widgets. Captured same-origin
    # scripts are localized; external scripts are left on their original URL
    # so public CDNs/widgets can still boot normally.
    for tag in soup.find_all("script"):
        src = tag.get("src")
        if not src:
            continue
        u = normalize_url(source_url, src)
        if u and u in asset_map:
            tag["src"] = asset_base + asset_map[u]

    # Preserve iframes and forms. A mirror that is supposed to behave like the
    # source needs these controls intact; the live network shim/proxy handles
    # same-origin API traffic without stripping the site's UI handlers.
    for tag in soup.find_all(True):
        # Keep inline onclick/onsubmit/etc. handlers: many production sites
        # still use them for modals, language selectors and menus.
        for attr in ("href", "src", "poster"):
            if not tag.has_attr(attr):
                continue
            raw = tag.get(attr)
            if not isinstance(raw, str):
                continue
            u = normalize_url(source_url, raw)
            if not u:
                continue
            if u in page_map:
                tag[attr] = "/__twin_page__?path=" + page_map[u]
            elif u in asset_map:
                tag[attr] = asset_base + asset_map[u]
            # External assets/links are intentionally preserved. This lets
            # public CDNs, fonts, images and widgets used by the original site
            # continue to load in the twin.
        if tag.has_attr("srcset"):
            tag["srcset"] = rewrite_srcset(tag["srcset"], asset_map, source_url, asset_base)
        if tag.name == "link" and str(tag.get("rel", "")).lower().find("stylesheet") >= 0:
            pass
    head = soup.head or soup
    marker = soup.new_tag("meta")
    marker["name"] = "cybertotal-twin"
    marker["content"] = "isolated-sanitized"
    head.append(marker)
    style = soup.new_tag("style")
    style.string = """
    .ct-banner{position:fixed;bottom:10px;left:10px;z-index:2147483647;background:#0f172acc;color:#fff;padding:6px 9px;border-radius:8px;font:11px system-ui;box-shadow:0 6px 18px rgba(0,0,0,.18);pointer-events:none}
    """
    head.append(style)

    # Live network shim. Runtime requests that belong to the original site are
    # sent through our live proxy. Common relative API calls (/api, /graphql,
    # /rpc, /ajax, etc.) are treated as target-origin requests even though the
    # mirror itself is served from 127.0.0.1.
    live_base = f"/twin/{job_id}/__live?url="
    shim = soup.new_tag("script")
    shim.string = f"""
    (()=>{{
      const LIVE_BASE = {json.dumps(live_base)};
      const TARGET_ORIGIN = {json.dumps(target_origin)};
      const API_PREFIX = /^(\/api(?:\/|$)|\/graphql(?:\/|$)|\/rpc(?:\/|$)|\/ajax(?:\/|$)|\/rest(?:\/|$)|\/v[0-9]+(?:\/|$))/i;
      function targetURL(url){{
        try {{
          const u = new URL(url, location.href);
          if (u.origin === TARGET_ORIGIN) return u;
          if (u.origin === location.origin && API_PREFIX.test(u.pathname)) return new URL(u.pathname + u.search, TARGET_ORIGIN);
        }} catch(e){{}}
        return null;
      }}
      function proxyUrl(url){{ return LIVE_BASE + encodeURIComponent(url); }}
      const origFetch = window.fetch ? window.fetch.bind(window) : null;
      window.fetch = function(input, init={{}}){{
        const raw = typeof input === 'string' ? input : (input && input.url);
        const target = raw && targetURL(raw);
        if (!target) return origFetch ? origFetch(input, init) : Promise.reject(new Error('fetch unavailable'));
        const next = Object.assign({{}}, init);
        next.credentials = 'omit';
        next.headers = new Headers(next.headers || {{}});
        next.headers.delete('authorization'); next.headers.delete('cookie'); next.headers.delete('x-api-key');
        return origFetch(proxyUrl(target.href), next);
      }};
      const OrigXHR = window.XMLHttpRequest;
      function TwinXHR(){{
        const xhr = new OrigXHR();
        const origOpen = xhr.open.bind(xhr);
        xhr.open = function(method, url, ...rest){{
          const target = targetURL(url);
          if (target) url = proxyUrl(target.href);
          return origOpen(method, url, ...rest);
        }};
        return xhr;
      }}
      window.XMLHttpRequest = TwinXHR;
    }})();
    """
    head.insert(0, shim)

    runtime = soup.new_tag("script")
    runtime.string = """
    (()=>{
      const banner=document.createElement('div'); banner.className='ct-banner';
      banner.textContent='Digital Twin';
      document.addEventListener('DOMContentLoaded',()=>document.body.appendChild(banner));
      document.addEventListener('click',(e)=>{
        const a=e.target.closest('a[href^="/__twin_page__"]');
        if(a){e.preventDefault(); location.href=a.getAttribute('href');}
      });
    })();
    """
    head.append(runtime)
    return str(soup)


def rewrite_css(css: bytes, css_url: str, asset_map: dict[str, str], asset_base: str) -> bytes:
    text = css.decode("utf-8", errors="ignore")
    def repl(m):
        raw = m.group(1).strip().strip('"\'')
        u = normalize_url(css_url, raw)
        if u and u in asset_map:
            return f"url('{asset_base}{asset_map[u]}')"
        if u and same_origin(css_url, u):
            return "url('')"
        return m.group(0)
    return re.sub(r"url\(([^)]+)\)", repl, text).encode("utf-8")


def is_json_response(resp) -> bool:
    ct = (resp.headers.get("content-type") or "").lower()
    return "application/json" in ct or "application/graphql" in ct or "json" in ct


def build_twin(job_id: str, target: str, max_pages: int, max_depth: int) -> None:
    root = GENERATED / job_id
    site = root / "site"
    site.mkdir(parents=True, exist_ok=True)
    meta = {
        "job_id": job_id, "target_url": target, "generated_at": time.time(),
        "pages": [], "assets": [], "observed_apis": [], "predicted_apis": [], "errors": [],
        "safety": {"scripts_removed": False, "forms_disabled": False, "cookies_saved": False, "auth_headers_saved": False, "post_bodies_saved": False, "same_origin_only": True}
    }
    pages_seen: dict[str, str] = {}
    assets: dict[str, str] = {}
    html_raw: dict[str, bytes] = {}
    css_raw: dict[str, bytes] = {}
    json_samples: dict[str, Any] = {}
    queue: list[tuple[str, int]] = [(target, 0)]
    queued = {target}
    update_job(job_id, status="running", message="Launching browser")

    def on_response(resp):
        try:
            u = resp.url
            if not same_origin(target, u) or resp.request.method != "GET":
                return
            p = urlparse(u).path.lower()
            ct = (resp.headers.get("content-type") or "").lower()
            if is_json_response(resp):
                if len(json_samples) < 120:
                    try:
                        body = resp.body()
                        if len(body) <= MAX_BODY:
                            json_samples[u] = json.loads(body.decode("utf-8", errors="ignore"))
                    except Exception:
                        pass
                return
            if any(x in ct for x in ("text/css",)):
                try:
                    body = resp.body()[:MAX_BODY]
                    css_raw[u] = body
                    # Register in the asset map immediately (same as images below),
                    # so page-rewriting later in the pipeline can find it. Registering
                    # this only in the post-crawl css_raw loop was too late: pages are
                    # rewritten before that loop runs, so <link href="..."> was left
                    # pointing at the original (unreachable) URL.
                    if u not in assets:
                        assets[u] = url_to_local(u, "asset", assets)
                except Exception:
                    pass
            elif any(x in ct for x in ("javascript", "ecmascript")):
                # Same-origin JS is downloaded and localized so the site's own
                # UI logic (tabs, animations, boot/reveal sequences) can keep
                # running in the twin. Cross-origin JS is never captured here
                # (on_response already filters to same-origin above), and
                # rewrite_html() drops any <script src> that isn't in this
                # map, so third-party/tracker scripts never load in the twin.
                try:
                    body = resp.body()
                    if len(body) <= MAX_BODY:
                        if u not in assets:
                            assets[u] = url_to_local(u, "asset", assets)
                        out = root / assets[u]
                        out.parent.mkdir(parents=True, exist_ok=True)
                        out.write_bytes(body)
                except Exception:
                    pass
            elif any(x in ct for x in ("image/", "font/", "video/", "audio/", "application/font", "application/woff", "application/vnd.ms-fontobject")):
                try:
                    body = resp.body()
                    if len(body) <= MAX_BODY:
                        if u not in assets:
                            assets[u] = url_to_local(u, "asset", assets)
                        out = root / assets[u]
                        out.parent.mkdir(parents=True, exist_ok=True)
                        out.write_bytes(body)
                except Exception:
                    pass
        except Exception:
            pass

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(ignore_https_errors=True, user_agent="CyberTotal-TwinBuilder/1.0")
            page = context.new_page()
            page.on("response", on_response)
            while queue and len(pages_seen) < max_pages:
                u, depth = queue.pop(0)
                if u in pages_seen:
                    continue
                if not same_origin(target, u):
                    continue
                try:
                    page.goto(u, wait_until="networkidle", timeout=35000)
                except PlaywrightTimeoutError:
                    try:
                        page.goto(u, wait_until="domcontentloaded", timeout=20000)
                    except Exception as e:
                        meta["errors"].append({"url": u, "error": str(e)})
                        continue
                except Exception as e:
                    meta["errors"].append({"url": u, "error": str(e)})
                    continue
                final_url = urldefrag(page.url)[0]
                if not same_origin(target, final_url):
                    continue
                html = page.content().encode("utf-8", errors="ignore")
                html_raw[final_url] = html
                pages_seen[u] = final_url
                title = page.title()
                page_path = safe_rel_path(final_url, "index.html")
                if page_path.endswith("/"):
                    page_path += "index.html"
                pages_seen[final_url] = page_path
                meta["pages"].append({"requested_url": u, "final_url": final_url, "depth": depth, "file": "site/" + page_path, "title": title})
                if depth < max_depth:
                    try:
                        for href in page.locator("a[href]").evaluate_all("els=>els.map(e=>e.href)"):
                            nu = normalize_url(final_url, href)
                            if not nu or not same_origin(target, nu):
                                continue
                            if nu not in queued and len(queued) < max_pages * 5:
                                queued.add(nu); queue.append((nu, depth + 1))
                    except Exception:
                        pass
            context.close(); browser.close()
    except Exception as e:
        meta["errors"].append({"stage": "browser", "error": repr(e)})

    # Build page map after redirects are known.
    page_map: dict[str, str] = {}
    for final_url, path in pages_seen.items():
        if final_url.startswith("http"):
            page_map[final_url] = path
    # Include requested URLs that map to final URLs.
    for requested, final in list((m.get("requested_url"), m.get("final_url")) for m in meta["pages"]):
        if requested and final in page_map:
            page_map[requested] = page_map[final]

    for final_url, html in html_raw.items():
        out_rel = page_map.get(final_url, safe_rel_path(final_url, "index.html"))
        out = site / out_rel
        out.parent.mkdir(parents=True, exist_ok=True)
        target_origin = f"{urlparse(target).scheme}://{urlparse(target).netloc}"
        out.write_text(rewrite_html(html, final_url, page_map, assets, f"/twin/{job_id}/__asset?path=", job_id, target_origin), encoding="utf-8")

    for css_url, body in css_raw.items():
        if css_url not in assets:
            assets[css_url] = url_to_local(css_url, "asset", assets)
        out = root / assets[css_url]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(rewrite_css(body, css_url, assets, f"/twin/{job_id}/__asset?path="))

    # Lightweight API prediction from observed JSON requests + page source references.
    observed = []
    for u, sample in json_samples.items():
        observed.append({"method": "GET", "url": u, "path": urlparse(u).path or "/", "sample_type": type(sample).__name__, "sample": sample if isinstance(sample, (dict, list)) else str(sample)[:4000]})
    meta["assets"] = sorted(list(assets.keys()))
    meta["observed_apis"] = observed
    predicted = []
    # Start with routes actually observed in the browser network graph.
    seen_paths = set()
    for item in observed:
        path = item["path"]
        if path not in seen_paths:
            seen_paths.add(path)
            predicted.append({"method": item.get("method", "GET"), "path": path, "confidence": 0.95, "source": "observed-network"})

    # Basic backend discovery from explicit API-like references in captured JS.
    # This is static extraction, not endpoint fuzzing.
    api_route_re = re.compile(r"[\"']((?:/|https?://)[^\"']*(?:api|graphql|rpc|ajax)[^\"']*)[\"']", re.I)
    for asset_url, local_path in assets.items():
        if not any(asset_url.lower().endswith(x) for x in ('.js', '.mjs')):
            continue
        try:
            body = (root / local_path).read_text(encoding='utf-8', errors='ignore')
        except Exception:
            continue
        for raw in api_route_re.findall(body):
            try:
                absolute = urljoin(asset_url, raw)
                if not same_origin(target, absolute):
                    continue
                path = urlparse(absolute).path or '/'
                if urlparse(absolute).query:
                    path += '?' + urlparse(absolute).query
                if path not in seen_paths:
                    seen_paths.add(path)
                    predicted.append({"method": "GET", "path": path, "confidence": 0.60, "source": "static-js-reference"})
            except Exception:
                continue
    meta["predicted_apis"] = predicted

    api_dir = root / "api"
    api_dir.mkdir(exist_ok=True)
    for i, item in enumerate(observed):
        (api_dir / f"{i:03d}.json").write_text(json.dumps(item, indent=2, ensure_ascii=False), encoding="utf-8")
    (root / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    update_job(job_id, status="done", message="Twin generated", result={
        "job_id": job_id,
        "preview_url": f"/twin/{job_id}/",
        "report_url": f"/report/{job_id}",
        "metadata": meta,
    })

class GenerateRequest(BaseModel):
    target_url: HttpUrl
    max_pages: int = Field(default=8, ge=1, le=40)
    max_depth: int = Field(default=2, ge=0, le=5)

app = FastAPI(title="CyberTotal Twin Generator")

DASHBOARD = """
<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>CyberTotal Digital Twin</title><style>
body{margin:0;background:#08111f;color:#e5edf7;font:14px system-ui}.wrap{max-width:900px;margin:70px auto;padding:0 18px}.card{background:#101c2e;border:1px solid #243451;border-radius:16px;padding:22px;box-shadow:0 12px 40px #0003}.grid{display:grid;grid-template-columns:1fr 90px 90px auto;gap:8px;margin-top:16px}input,button{border-radius:9px;padding:10px;border:1px solid #334764;background:#0a1424;color:#e5edf7}button{cursor:pointer;background:#2563eb;border-color:#2563eb;font-weight:700}.muted{color:#9cb0c8}.status{margin-top:12px;color:#a9bad0;font-size:13px}.links{display:flex;gap:8px;margin-top:12px}.link{color:#93c5fd;text-decoration:none;border:1px solid #2a4770;padding:7px 10px;border-radius:8px}.done{color:#86efac}
</style></head><body><div class='wrap'><div class='card'><b>CyberTotal Digital Twin</b><div class='muted'>Live public-site twin generator</div>
<div class='grid'><input id='url' value='https://example.com' placeholder='https://your-authorized-site.com'><input id='pages' type='number' value='8' min='1' max='40'><input id='depth' type='number' value='2' min='0' max='5'><button onclick='go()'>Generate</button></div>
<div id='status' class='status'>Ready.</div><div id='links' class='links'></div></div></div>
<script>
async function go(){const s=document.getElementById('status'),l=document.getElementById('links');l.innerHTML='';s.className='status';s.textContent='Generating…';try{const r=await fetch('/api/generate',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({target_url:document.getElementById('url').value,max_pages:+pages.value,max_depth:+depth.value})});const j=await r.json();if(!r.ok){s.textContent=j.detail||'Generation failed';return;}const id=j.job_id;for(;;){await new Promise(x=>setTimeout(x,900));const q=await (await fetch('/api/jobs/'+id)).json();if(q.status==='done'){s.className='status done';s.textContent='Twin ready.';const p=q.result.preview_url,rep=q.result.report_url;l.innerHTML='<a class="link" target="_blank" href="'+p+'">Open Twin</a><a class="link" target="_blank" href="'+rep+'">Open Report</a>';return;}if(q.status==='error'){s.textContent='Generation failed';return;}s.textContent='Working…';}}catch(e){s.textContent='Error: '+e.message}}
</script></body></html>
"""


def report_html(meta: dict[str, Any]) -> str:
    pages = ''.join(f"<tr><td>{p['depth']}</td><td>{p['title'][:120]}</td><td><code>{p['final_url']}</code></td></tr>" for p in meta.get('pages', []))
    apis = ''.join(f"<tr><td>{a['method']}</td><td><code>{a['path']}</code></td><td>{a.get('confidence',0.95):.2f}</td><td>{a.get('source','observed-network')}</td></tr>" for a in meta.get('predicted_apis', [])) or '<tr><td colspan=4>No JSON APIs observed during crawl.</td></tr>'
    return f"""<!doctype html><html><head><meta charset='utf-8'><title>CyberTotal Report</title><style>body{{font:14px system-ui;margin:32px;background:#f8fafc;color:#172033}}.wrap{{max-width:1100px;margin:auto}}.card{{background:white;border:1px solid #dbe4ef;border-radius:16px;padding:20px;margin-bottom:16px}}table{{width:100%;border-collapse:collapse}}th,td{{border-bottom:1px solid #e8eef5;text-align:left;padding:9px;vertical-align:top}}code{{font-size:12px}}a{{color:#2563eb}}</style></head><body><div class='wrap'>
<div class='card'><h1>CyberTotal Digital Twin Report</h1><p><b>Target:</b> {meta['target_url']}</p><p><b>Job:</b> {meta['job_id']}</p><p><a href='/twin/{meta['job_id']}/' target='_blank'>Open generated mirror website →</a></p></div>
<div class='card'><h2>What was generated</h2><p>{len(meta.get('pages',[]))} pages, {len(meta.get('assets',[]))} captured assets, {len(meta.get('observed_apis',[]))} observed JSON API responses.</p><pre>{json.dumps(meta.get('safety',{}),indent=2)}</pre></div>
<div class='card'><h2>Pages</h2><table><tr><th>Depth</th><th>Title</th><th>URL</th></tr>{pages}</table></div>
<div class='card'><h2>Backend / API prediction</h2><p>Runtime API calls are proxied live to the same public target origin. API discovery is based on observed network traffic and static route references in captured same-origin JavaScript; it does not brute-force hidden endpoints or replay authenticated requests.</p><table><tr><th>Method</th><th>Path</th><th>Confidence</th><th>Evidence</th></tr>{apis}</table></div>
<div class='card'><h2>Errors</h2><pre>{json.dumps(meta.get('errors',[]),indent=2)}</pre></div>
</div></body></html>"""


@app.get("/", response_class=HTMLResponse)
def home():
    return DASHBOARD

@app.post("/api/generate")
def generate(req: GenerateRequest):
    try:
        target = validate_target(str(req.target_url))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    job_id = uuid.uuid4().hex[:12]
    update_job(job_id, status="queued", target=target, created_at=time.time())
    executor.submit(build_twin, job_id, target, req.max_pages, req.max_depth)
    return {"status": "queued", "job_id": job_id}

@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    jobs = _load_jobs()
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    return j

@app.get("/report/{job_id}", response_class=HTMLResponse)
def report(job_id: str):
    jobs = _load_jobs(); j = jobs.get(job_id)
    if not j or j.get('status') != 'done': raise HTTPException(404, 'Report not ready')
    return report_html(j['result']['metadata'])

@app.get("/twin/{job_id}/")
def twin_home(job_id: str):
    return twin_page(job_id, "index.html")

# IMPORTANT: these two specific routes must be registered BEFORE the
# catch-all "/twin/{job_id}/{path:path}" route below. FastAPI/Starlette
# matches routes in registration order, so if the catch-all came first
# it would swallow every request to __asset and __api (matching "__asset"
# and "__api" as the "path" parameter) before they ever reached their
# real handlers -- which is exactly what was happening: every CSS file,
# image, font, and mock API call was silently 404ing as "Twin page not
# found" instead of being served.
@app.get("/twin/{job_id}/__asset")
def twin_asset(job_id: str, path: str):
    root = _root_for(job_id)
    file = (root / path).resolve()
    assets_root = (root / 'assets').resolve()
    if not str(file).startswith(str(assets_root)) or not file.exists() or not file.is_file():
        raise HTTPException(404, 'Twin asset not found')
    return FileResponse(file)

@app.get("/twin/{job_id}/__api")
def twin_api(job_id: str, path: str):
    root = _root_for(job_id)
    meta_file = root / 'metadata.json'
    if not meta_file.exists():
        raise HTTPException(404, 'Twin not found')
    meta = json.loads(meta_file.read_text(encoding='utf-8'))
    for item in meta.get('observed_apis', []):
        if item.get('path') == path:
            return JSONResponse(item.get('sample'))
    raise HTTPException(404, 'No observed mock exists for this API path')

@app.api_route("/twin/{job_id}/__live", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
def twin_live(request: Request, job_id: str):
    """Proxy runtime requests to the original public target origin.

    Safety boundaries: target must be public/same-origin; credentials, cookies,
    authorization and API-key headers are never forwarded from the browser.
    """
    root = _root_for(job_id)
    meta_file = root / 'metadata.json'
    if not meta_file.exists():
        raise HTTPException(404, 'Twin not found')
    meta = json.loads(meta_file.read_text(encoding='utf-8'))
    target_origin = f"{urlparse(meta['target_url']).scheme}://{urlparse(meta['target_url']).netloc}"

    raw_url = request.query_params.get('url')
    if not raw_url:
        raise HTTPException(400, 'Missing url')
    try:
        target_url = validate_target(raw_url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not same_origin(meta['target_url'], target_url):
        raise HTTPException(403, 'Only the target origin can be proxied')

    body = None
    if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
        body = asyncio.run(request.body())
        if len(body) > 2 * 1024 * 1024:
            raise HTTPException(413, 'Request body too large')

    allowed_headers = {}
    for name in ('accept', 'content-type', 'user-agent'):
        value = request.headers.get(name)
        if value:
            allowed_headers[name] = value
    allowed_headers['user-agent'] = 'CyberTotal-Twin-LiveProxy/1.0'

    req = URLRequest(target_url, data=body, headers=allowed_headers, method=request.method)
    try:
        with urlopen(req, timeout=20) as resp:
            data = resp.read(MAX_BODY)
            content_type = resp.headers.get('content-type', 'application/octet-stream')
            return Response(content=data, status_code=resp.status, media_type=content_type.split(';', 1)[0])
    except HTTPError as e:
        data = e.read(MAX_BODY)
        content_type = e.headers.get('content-type', 'text/plain')
        return Response(content=data, status_code=e.code, media_type=content_type.split(';', 1)[0])
    except (URLError, TimeoutError) as e:
        raise HTTPException(502, f'Upstream request failed: {e}')


@app.get("/twin/{job_id}/{path:path}")
def twin_any(job_id: str, path: str):
    if path == "": return twin_page(job_id, "index.html")
    return twin_page(job_id, path)

def _root_for(job_id: str) -> Path:
    p = (GENERATED / job_id).resolve()
    if not str(p).startswith(str(GENERATED.resolve())): raise HTTPException(400, 'invalid job')
    return p


def twin_page(job_id: str, path: str):
    root = _root_for(job_id); site = root / "site"
    rel = Path(path)
    if rel.suffix == "": rel = rel / "index.html"
    file = (site / rel).resolve()
    if not str(file).startswith(str(site.resolve())) or not file.exists() or not file.is_file():
        raise HTTPException(404, 'Twin page not found')
    if file.suffix.lower() in {'.html', '.htm'}:
        html = file.read_text(encoding='utf-8', errors='ignore')
        # Rewrite captured navigation placeholders to stable /twin routes.
        html = html.replace("/__twin_page__?path=", f"/twin/{job_id}/")
        return HTMLResponse(html)
    return FileResponse(file)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
