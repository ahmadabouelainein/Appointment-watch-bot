#!/usr/bin/env python3
"""
Appointment watch + auto-book bot for the LRA Reutlingen smartCJM calendar
(Zulassungsstelle). Built to run on a spare Android phone (Termux) on a home
internet connection, because the site's Cloudflare blocks datacenter IPs
(GitHub Actions included).

Each cycle starts a brand-new session from the calendar `uid`, so the rotating
`wsid` in the link and the ~20 minute session timeout don't matter.

Modes:
  python bot.py discover   walk the flow once, save every page, send a summary
                           (+ a zip of the pages) to your ntfy topic. Never books.
  python bot.py watch      poll forever; alert on free slots and, if AUTO_BOOK=1,
                           book the first matching one with your name + email.

Configuration lives in config.env next to this file (never committed).
"""
import json
import os
import random
import re
import sys
import time
import traceback
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
DUMPS = HERE / "dumps"
STATE = HERE / "state.json"


# --------------------------------------------------------------------- config
def load_config():
    cfg = {
        "BASE_URL": "https://lra-reutlingen.saas.smartcjm.com/m/bsc/extern/calendar",
        "CALENDAR_UID": "b7bab144-58a7-4044-ad6f-505e604f6401",
        "SERVICE_INDEX": "3",        # "option 3" on the first page (1-based)
        "SERVICE_NAME": "",          # optional: substring of the service name, overrides index
        "LOCATION_NAME": "",         # optional: substring of a location, else first one
        "FIRST_NAME": "",
        "LAST_NAME": "",
        "EMAIL": "",
        "PHONE": "",
        "AUTO_BOOK": "1",
        "NOT_BEFORE": "",            # optional YYYY-MM-DD: ignore slots earlier than this
        "NOT_AFTER": "",             # optional YYYY-MM-DD: ignore slots later than this
        "POLL_SECONDS": "75",
        "NTFY_TOPIC": "",
        "NTFY_SERVER": "https://ntfy.sh",
    }
    f = HERE / "config.env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in list(cfg):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


CFG = load_config()


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------------- ntfy
def notify(title, msg, priority="default", click=None, tags=None):
    topic = CFG["NTFY_TOPIC"]
    log(f"[notify] {title}: {msg[:300]}")
    if not topic:
        return
    headers = {"Title": title.encode("utf-8"), "Priority": priority}
    if click:
        headers["Click"] = click
    if tags:
        headers["Tags"] = tags
    try:
        requests.post(f"{CFG['NTFY_SERVER']}/{topic}", data=msg.encode("utf-8"),
                      headers=headers, timeout=20)
    except Exception as e:
        log("ntfy failed:", e)


def notify_file(path, message):
    topic = CFG["NTFY_TOPIC"]
    if not topic:
        return
    try:
        with open(path, "rb") as fh:
            requests.put(f"{CFG['NTFY_SERVER']}/{topic}", data=fh,
                         headers={"Filename": Path(path).name, "Message": message.encode("utf-8")},
                         timeout=60)
    except Exception as e:
        log("ntfy upload failed:", e)


# --------------------------------------------------------------------- dumps
class Dumper:
    def __init__(self, enabled_full):
        self.full = enabled_full
        self.n = 0
        self.run = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.files = []

    def save(self, label, resp):
        self.n += 1
        DUMPS.mkdir(exist_ok=True)
        ext = "json" if "json" in resp.headers.get("content-type", "") else "html"
        p = DUMPS / f"{self.run}-{self.n:02d}-{label}.{ext}"
        meta = f"<!-- {resp.request.method} {resp.url} -> {resp.status_code} -->\n"
        if resp.request.body and self.full:
            body = resp.request.body
            if isinstance(body, bytes):
                body = body.decode("utf-8", "replace")
            meta += f"<!-- BODY {redact(body)} -->\n"
        p.write_text(meta + resp.text, encoding="utf-8")
        self.files.append(p)
        # keep the dumps folder small on the phone
        old = sorted(DUMPS.glob("*"))[:-200]
        for o in old:
            o.unlink(missing_ok=True)
        return p


def redact(s):
    for k in ("FIRST_NAME", "LAST_NAME", "EMAIL", "PHONE"):
        v = CFG.get(k)
        if v:
            s = s.replace(v, f"<{k}>").replace(requests.utils.quote(v), f"<{k}>")
    return s


# --------------------------------------------------------------------- forms
META_NAMES = {"__requestverificationtoken", "uid", "wsid", "lang", "rev", "action", "step"}
SLOT_TIME = re.compile(r"\b([01]?\d|2[0-3])[:.][0-5]\d\b")
DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
NO_SLOTS = re.compile(r"kein(e|en)?\s+(freie[nr]?\s+)?termin|keine\s+termine|nicht\s+verf[üu]gbar|"
                      r"no\s+(free\s+)?appointments?", re.I)
EXPIRED = re.compile(r"session\s+abgelaufen|sitzung\s+(ist\s+)?abgelaufen", re.I)
SUCCESS = re.compile(r"erfolgreich\s+(gebucht|reserviert)|termin\s+wurde|buchungsnummer|"
                     r"vielen\s+dank|best[äa]tigung|reservierungsnummer|vorgangsnummer", re.I)


def soup_of(resp):
    return BeautifulSoup(resp.text, "html.parser")


def label_for(soup, el):
    """Best-effort human text describing a form control."""
    texts = []
    if el.get("id"):
        lab = soup.find("label", attrs={"for": el["id"]})
        if lab:
            texts.append(lab.get_text(" ", strip=True))
    par = el.find_parent("label")
    if par:
        texts.append(par.get_text(" ", strip=True))
    for attr in ("aria-label", "title", "placeholder", "data-name"):
        if el.get(attr):
            texts.append(el[attr])
    if not texts:
        # walk up to a container that has some text
        node = el
        for _ in range(5):
            node = node.parent
            if node is None:
                break
            t = node.get_text(" ", strip=True)
            if t:
                texts.append(t[:200])
                break
    return " | ".join(texts)


def form_defaults(form):
    data = []
    for el in form.find_all(["input", "select", "textarea"]):
        name = el.get("name")
        if not name or el.has_attr("disabled"):
            continue
        t = (el.get("type") or "text").lower()
        if el.name == "select":
            opt = el.find("option", selected=True) or el.find("option")
            data.append([name, opt.get("value", opt.get_text(strip=True)) if opt else ""])
        elif t in ("checkbox", "radio"):
            if el.has_attr("checked"):
                data.append([name, el.get("value", "on")])
        elif t in ("submit", "button", "image", "reset", "file"):
            continue
        else:
            data.append([name, el.get("value", "")])
    return data


def set_field(data, name, value):
    for pair in data:
        if pair[0] == name:
            pair[1] = value
            return
    data.append([name, value])


def pick_submit(form, prefer=r"weiter|suchen|termin|next|continue|buchen|absenden|reservieren|best[äa]tigen"):
    btns = form.find_all(["button", "input"])
    btns = [b for b in btns if (b.get("type") or ("submit" if b.name == "button" else "")).lower() == "submit"]
    for b in btns:
        txt = b.get_text(" ", strip=True) or b.get("value", "")
        if re.search(prefer, txt, re.I):
            return b
    return btns[0] if btns else None


def submit(s, page_url, form, data, button=None):
    action = urljoin(page_url, form.get("action") or page_url)
    method = (form.get("method") or "get").lower()
    data = [tuple(p) for p in data]
    if button is not None and button.get("name"):
        data.append((button["name"], button.get("value", "")))
    if button is not None and button.get("formaction"):
        action = urljoin(page_url, button["formaction"])
    if method == "post":
        return s.post(action, data=data, timeout=30)
    return s.get(action, params=data, timeout=30)


def main_form(soup):
    forms = soup.find_all("form")
    if not forms:
        return None
    for f in forms:
        if f.find("input", attrs={"name": "__RequestVerificationToken"}):
            return f
    return forms[0]


def describe_page(soup, url):
    out = [f"URL: {url}", f"TITLE: {soup.title.get_text(strip=True) if soup.title else ''}"]
    for i, f in enumerate(soup.find_all("form")):
        out.append(f"FORM#{i} name={f.get('name')} method={f.get('method')} action={f.get('action')}")
        for el in f.find_all(["input", "select", "textarea", "button"]):
            out.append(f"   {el.name} type={el.get('type')} name={el.get('name')} "
                       f"value={(el.get('value') or '')[:40]} :: {label_for(soup, el)[:120]}")
    txt = soup.get_text(" ", strip=True)
    out.append("TEXT: " + txt[:1500])
    return "\n".join(out)


# ------------------------------------------------------------------- the flow
class Flow:
    def __init__(self, dumper):
        self.d = dumper
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 6) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36",
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.6",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        self.notes = []

    def get(self, label, url, **kw):
        r = self.s.get(url, timeout=30, **kw)
        self.d.save(label, r)
        return r

    def check_block(self, r):
        if r.status_code == 403 and "cloudflare" in r.text.lower():
            raise Blocked("Cloudflare blocked this connection (403).")

    # 1. fresh session -> services page
    def start(self):
        base = CFG["BASE_URL"].rstrip("/") + "/"
        r = self.get("start", base, params={"uid": CFG["CALENDAR_UID"], "lang": "de"})
        self.check_block(r)
        q = parse_qs(urlparse(r.url).query)
        self.wsid = (q.get("wsid") or [None])[0]
        if not self.wsid:
            m = re.search(r"wsid=([0-9a-f-]{36})", r.text)
            self.wsid = m.group(1) if m else None
            if self.wsid:
                r = self.get("services", base, params={"uid": CFG["CALENDAR_UID"],
                                                       "wsid": self.wsid, "lang": "de"})
        self.notes.append(f"wsid={self.wsid}")
        return r

    def manual_link(self):
        return (f"{CFG['BASE_URL'].rstrip('/')}/?uid={CFG['CALENDAR_UID']}"
                + (f"&wsid={self.wsid}" if getattr(self, "wsid", None) else "") + "&lang=de")

    # 2. pick "option 3"
    def choose_service(self, r):
        soup = soup_of(r)
        form = main_form(soup)
        if form is None:
            raise FlowError("No form on the services page.\n" + describe_page(soup, r.url))
        data = form_defaults(form)
        cands = []
        for el in form.find_all(["input", "select"]):
            name = el.get("name") or ""
            t = (el.get("type") or "text").lower()
            if not name or name.lower() in META_NAMES:
                continue
            if el.name == "select" or t in ("number", "checkbox", "radio") or \
                    (t in ("hidden", "text") and (el.get("value") or "") in ("0", "")
                     and re.search(r"service|anliegen|concern|count|amount|anzahl|quant", name, re.I)):
                cands.append(el)
        # one entry per distinct name (radios share a name; then each radio is a choice)
        if not cands:
            raise FlowError("Could not find the service choices.\n" + describe_page(soup, r.url))
        labels = [label_for(soup, c) for c in cands]
        idx = None
        if CFG["SERVICE_NAME"]:
            for i, lab in enumerate(labels):
                if CFG["SERVICE_NAME"].lower() in lab.lower():
                    idx = i
                    break
        if idx is None:
            idx = int(CFG["SERVICE_INDEX"]) - 1
        if idx >= len(cands):
            raise FlowError(f"Only {len(cands)} services found, wanted #{idx+1}.\n" + describe_page(soup, r.url))
        el = cands[idx]
        self.service_label = labels[idx][:120]
        self.notes.append("service: " + self.service_label)
        self.notes.append("all services: " + " || ".join(f"{i+1}) {l[:70]}" for i, l in enumerate(labels)))
        t = (el.get("type") or "text").lower()
        if el.name == "select":
            opts = [o.get("value", o.get_text(strip=True)) for o in el.find_all("option")]
            val = "1" if "1" in opts else (opts[1] if len(opts) > 1 else opts[0])
            set_field(data, el["name"], val)
        elif t in ("checkbox", "radio"):
            if t == "radio":
                data = [p for p in data if p[0] != el["name"]]
            data.append([el["name"], el.get("value", "on")])
        else:
            set_field(data, el["name"], "1")
        resp = submit(self.s, r.url, form, data, pick_submit(form))
        self.d.save("after-service", resp)
        return resp

    # 3. walk until we reach slots / no slots
    def to_slots(self, r):
        for step in range(6):
            self.check_block(r)
            if EXPIRED.search(r.text):
                raise Expired()
            slots = find_slots(r)
            if slots:
                return r, slots
            soup = soup_of(r)
            text = soup.get_text(" ", strip=True)
            if NO_SLOTS.search(text):
                return r, []
            form = main_form(soup)
            if form is None:
                return r, []
            # location step or "continue" step: pick a radio/select if any, then submit
            data = form_defaults(form)
            radios = [i for i in form.find_all("input", attrs={"type": "radio"}) if i.get("name")]
            if radios:
                choice = radios[0]
                if CFG["LOCATION_NAME"]:
                    for rd in radios:
                        if CFG["LOCATION_NAME"].lower() in label_for(soup, rd).lower():
                            choice = rd
                            break
                data = [p for p in data if p[0] != choice["name"]]
                data.append([choice["name"], choice.get("value", "on")])
                self.notes.append("location: " + label_for(soup, choice)[:80])
            # tick any plain confirmation checkboxes ("Hinweise gelesen")
            for cb in form.find_all("input", attrs={"type": "checkbox"}):
                if cb.get("name") and not cb.has_attr("checked"):
                    data.append([cb["name"], cb.get("value", "on")])
            r = submit(self.s, r.url, form, data, pick_submit(form))
            self.d.save(f"step{step}", r)
        return r, find_slots(r)

    # 4. book
    def book(self, page_resp, slot):
        if slot["kind"] == "link":
            r = self.get("slot", urljoin(page_resp.url, slot["href"]))
        else:
            r = submit(self.s, page_resp.url, slot["form"], slot["data"], slot.get("button"))
            self.d.save("slot", r)
        for step in range(4):
            self.check_block(r)
            soup = soup_of(r)
            text = soup.get_text(" ", strip=True)
            if SUCCESS.search(text) and not soup.find("input", attrs={"type": "email"}):
                return True, text[:800], r.url
            form = main_form(soup)
            if form is None:
                return False, "No booking form found: " + text[:600], r.url
            data = form_defaults(form)
            missing = []
            for el in form.find_all(["input", "select", "textarea"]):
                name = el.get("name")
                if not name or name.lower() in META_NAMES:
                    continue
                t = (el.get("type") or "text").lower()
                if t in ("hidden", "submit", "button"):
                    continue
                lab = (label_for(soup, el) + " " + name).lower()
                val = None
                if t == "checkbox":
                    if not el.has_attr("checked"):
                        data.append([name, el.get("value", "on")])  # consent / Datenschutz
                    continue
                if t == "radio":
                    continue
                if re.search(r"e-?mail", lab):
                    val = CFG["EMAIL"]
                elif re.search(r"vorname|first", lab):
                    val = CFG["FIRST_NAME"]
                elif re.search(r"nachname|familienname|last|surname", lab):
                    val = CFG["LAST_NAME"]
                elif re.search(r"\bname\b", lab):
                    val = f"{CFG['FIRST_NAME']} {CFG['LAST_NAME']}".strip()
                elif re.search(r"telefon|phone|mobil|handy", lab):
                    val = CFG["PHONE"]
                elif re.search(r"anrede|salutation", lab) and el.name == "select":
                    continue  # keep default
                elif el.has_attr("required") or el.get("aria-required") == "true":
                    missing.append(lab[:60])
                if val is not None:
                    if not val and (el.has_attr("required") or el.get("aria-required") == "true"):
                        missing.append(lab[:60])
                    set_field(data, name, val)
            if missing:
                return False, "Booking form needs fields I don't have: " + "; ".join(missing), r.url
            r = submit(self.s, r.url, form, data, pick_submit(form))
            self.d.save(f"book{step}", r)
        text = soup_of(r).get_text(" ", strip=True)
        return bool(SUCCESS.search(text)), text[:800], r.url


class FlowError(Exception):
    pass


class Expired(Exception):
    pass


class Blocked(Exception):
    pass


def find_slots(r):
    """Return a list of bookable slots found in a page (links, buttons, or JSON)."""
    slots = []
    ct = r.headers.get("content-type", "")
    if "json" in ct:
        try:
            js = r.json()
        except ValueError:
            js = None
        stack = [js]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                if isinstance(x.get("link"), str):
                    slots.append({"kind": "link", "href": x["link"],
                                  "text": " ".join(str(x.get(k, "")) for k in ("date", "time", "start", "label"))})
                stack.extend(x.values())
            elif isinstance(x, list):
                stack.extend(x)
        return filter_dates(slots)
    soup = soup_of(r)
    for a in soup.find_all("a", href=True):
        txt = a.get_text(" ", strip=True)
        href = a["href"]
        if re.search(r"booking|buchung|reserv|slot|appointment|termin=", href, re.I) or \
                (SLOT_TIME.search(txt) and not href.startswith("#")):
            if re.search(r"impressum|datenschutz|privacy|cancel|storn", href + txt, re.I):
                continue
            slots.append({"kind": "link", "href": href, "text": context_text(a)})
    for form in soup.find_all("form"):
        for b in form.find_all(["button", "input"]):
            if (b.get("type") or "submit").lower() not in ("submit",) and b.name == "input":
                continue
            txt = b.get_text(" ", strip=True) or b.get("value", "")
            if SLOT_TIME.search(txt) and b.get("name"):
                slots.append({"kind": "form", "form": form, "data": form_defaults(form),
                              "button": b, "text": context_text(b)})
    return filter_dates(slots)


def context_text(el):
    own = (el.get_text(" ", strip=True) or el.get("value", "")).strip()
    if DATE_RE.search(own):
        return own[:200]
    for attr in ("title", "aria-label", "data-date"):
        if el.get(attr) and DATE_RE.search(el[attr]):
            return f"{el[attr]} {own}"[:200]
    prev = el.find_previous(string=DATE_RE)
    date = DATE_RE.search(prev).group(0) if prev else ""
    return f"{date} {own}".strip()[:200]


def filter_dates(slots):
    lo, hi = CFG["NOT_BEFORE"], CFG["NOT_AFTER"]
    if not (lo or hi):
        return slots
    keep = []
    for s in slots:
        m = DATE_RE.search(s["text"])
        if not m:
            keep.append(s)
            continue
        iso = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
        if (not lo or iso >= lo) and (not hi or iso <= hi):
            keep.append(s)
    return keep


# ---------------------------------------------------------------------- modes
def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save_state(st):
    STATE.write_text(json.dumps(st, indent=1))


def one_cycle(dumper, allow_book):
    f = Flow(dumper)
    r = f.start()
    r = f.choose_service(r)
    page, slots = f.to_slots(r)
    return f, page, slots


def discover():
    d = Dumper(enabled_full=True)
    summary = []
    try:
        f, page, slots = one_cycle(d, allow_book=False)
        summary += f.notes
        summary.append(f"slots found: {len(slots)}")
        for s in slots[:10]:
            summary.append("  - " + s["text"][:100])
    except Exception as e:
        summary.append("ERROR: " + repr(e))
        summary.append(traceback.format_exc()[-1500:])
    for p in d.files:
        try:
            soup = BeautifulSoup(p.read_text(encoding="utf-8"), "html.parser")
            summary.append(f"\n##### {p.name}\n" + describe_page(soup, p.name)[:2500])
        except Exception:
            pass
    text = "\n".join(summary)
    out = DUMPS / f"discover-{d.run}.txt"
    DUMPS.mkdir(exist_ok=True)
    out.write_text(text, encoding="utf-8")
    z = DUMPS / f"discover-{d.run}.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(out, out.name)
        for p in d.files:
            zf.write(p, p.name)
    print(text)
    notify("Discovery finished", "\n".join(summary[:6])[:1000], tags="mag")
    notify_file(z, f"Discovery dump {d.run}")
    print(f"\nSaved {z}")


def watch():
    st = load_state()
    if st.get("booked"):
        log("Already booked:", st["booked"], "- delete state.json to watch again.")
        return
    auto = CFG["AUTO_BOOK"] == "1"
    if auto and not (CFG["EMAIL"] and (CFG["LAST_NAME"] or CFG["FIRST_NAME"])):
        log("AUTO_BOOK=1 but FIRST_NAME/LAST_NAME/EMAIL missing in config.env")
        sys.exit(1)
    notify("Bot started", f"Watching for appointments (auto-book: {'on' if auto else 'off'}).", tags="robot")
    errors = 0
    last_alert = ""
    cycle = 0
    while True:
        cycle += 1
        d = Dumper(enabled_full=False)
        try:
            f, page, slots = one_cycle(d, allow_book=auto)
            errors = 0
            if not slots:
                log(f"#{cycle} no free slots ({getattr(f, 'service_label', '')[:40]})")
                # keep only the last cycle's dumps when nothing happened
                for p in d.files[:-1]:
                    p.unlink(missing_ok=True)
            else:
                desc = "\n".join(s["text"][:80] for s in slots[:8])
                log(f"#{cycle} FOUND {len(slots)} slot(s):\n{desc}")
                if desc != last_alert:
                    notify("Free appointment found!", desc, priority="urgent",
                           click=f.manual_link(), tags="rotating_light")
                    last_alert = desc
                if auto:
                    ok, info, url = f.book(page, slots[0])
                    if ok:
                        st["booked"] = {"when": datetime.now().isoformat(), "slot": slots[0]["text"],
                                        "info": info[:500]}
                        save_state(st)
                        notify("BOOKED ✅", f"{slots[0]['text'][:100]}\n\n{info[:600]}\n\n"
                               "Check your email - you may need to click a confirmation link.",
                               priority="urgent", tags="white_check_mark")
                        log("Booked. Stopping.")
                        return
                    notify("Booking attempt failed", f"{info[:700]}\nBook manually NOW via the link.",
                           priority="urgent", click=f.manual_link(), tags="warning")
        except Blocked as e:
            errors += 1
            log("BLOCKED:", e)
            if errors in (1, 10):
                notify("Bot is blocked", "Cloudflare refuses this connection. Is the phone on home Wi-Fi?",
                       priority="high", tags="no_entry")
        except Expired:
            log("session expired mid-flow, retrying")
            continue
        except Exception as e:
            errors += 1
            log("error:", repr(e))
            if errors in (3, 30, 300):
                notify("Bot error", f"{e!r}"[:900], priority="high", tags="warning")
        wait = int(CFG["POLL_SECONDS"]) * random.uniform(0.8, 1.25)
        if errors:
            wait = min(900, wait * (1 + errors))
        time.sleep(wait)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "watch"
    {"discover": discover, "watch": watch}[mode]()
