"""What the Explorer reads off a live page: facts, links, clickable things, and forms."""

import datetime as _dt
import re

# Shared in-page helpers. textFor() is the single definition of "what an element
# says", used both when crawling and when the recorder checks the live page.
HELPERS_JS = r"""
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const ownText = el => norm(Array.from(el.childNodes).filter(n => n.nodeType === 3).map(n => n.textContent).join(' '));
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity) > 0;
  };
  const INTERACTIVE = 'a[href],button,[role=button],[role=link],[role=tab],input,select,textarea,summary,[onclick],[data-href]';
  const isField = el => ['input', 'select', 'textarea'].includes(el.tagName.toLowerCase())
    && !['submit', 'button', 'hidden'].includes(el.type);
  const labelFor = el => {
    if (el.labels && el.labels.length) return norm(Array.from(el.labels).map(l => ownText(l) || l.innerText).join(' '));
    return norm(el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('name') || '');
  };
  const textFor = el => {
    const tag = el.tagName.toLowerCase();
    if (tag === 'label') return ownText(el);
    if (isField(el)) return labelFor(el);
    if (el.matches(INTERACTIVE)) return norm(el.innerText || el.value || el.getAttribute('aria-label'));
    return ownText(el);
  };
  const rectOf = el => { const r = el.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; };
  const feedbackEl = () => {
    for (const sel of ['[role=status]', '[role=alert]', '[role=dialog]', '.toast', '.modal', '[aria-live]']) {
      for (const el of document.querySelectorAll(sel)) if (visible(el) && norm(el.innerText)) return el;
    }
    return null;
  };
"""

# Runs inside the page. Returns visible elements with a stable CSS selector,
# their text, where they point, and their position (for zooming in on them later).
EXTRACT_JS = r"""
() => {
""" + HELPERS_JS + r"""
  const MAX = 400;
  const unique = sel => { try { return document.querySelectorAll(sel).length === 1; } catch (e) { return false; } };
  function selectorFor(el) {
    if (el.id && unique('#' + CSS.escape(el.id))) return '#' + CSS.escape(el.id);
    const tag = el.tagName.toLowerCase();
    for (const attr of ['data-testid', 'data-test', 'name', 'aria-label', 'data-href']) {
      const v = el.getAttribute(attr);
      if (v) { const sel = `${tag}[${attr}="${CSS.escape(v)}"]`; if (unique(sel)) return sel; }
    }
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && node !== document.body) {
      if (node.id && unique('#' + CSS.escape(node.id))) { parts.unshift('#' + CSS.escape(node.id)); break; }
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter(c => c.tagName === node.tagName);
        if (same.length > 1) part += `:nth-of-type(${same.indexOf(node) + 1})`;
      }
      parts.unshift(part);
      node = parent;
    }
    if (!parts.length || !parts[0].startsWith('#')) parts.unshift('body');
    return parts.join(' > ');
  }
  const navTarget = el => {
    if (el.tagName === 'A' && el.getAttribute('href')) return el.href;
    const dh = el.getAttribute('data-href');
    if (dh) return new URL(dh, location.href).href;
    const m = (el.getAttribute('onclick') || '').match(/location(?:\.href)?\s*=\s*['"]([^'"]+)['"]/);
    return m ? new URL(m[1], location.href).href : null;
  };
  const PRICE = /([$€£₹]\s?\d[\d,.]*|\b\d[\d,.]*\s?(usd|eur|inr|\/\s?mo(nth)?)\b)/i;
  const SEL = 'h1,h2,h3,h4,p,li,label,td,th,dt,dd,span,strong,small,div,' + INTERACTIVE;
  const out = [];
  for (const el of document.querySelectorAll(SEL)) {
    if (out.length >= MAX) break;
    if (!visible(el)) continue;
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role') || '';
    const isInteractive = el.matches(INTERACTIVE);
    if (isInteractive && el.parentElement && el.parentElement.closest('a[href],button,[role=button]')) continue;
    if (tag === 'input' && el.type === 'hidden') continue;
    const field = isField(el);
    const text = textFor(el);
    if (!text && !field) continue;
    if (!isInteractive && ['span', 'strong', 'small', 'td', 'dd', 'div'].includes(tag) && text.length > 160) continue;
    let kind = 'text';
    if (/^h[1-4]$/.test(tag)) kind = 'heading';
    else if (field) kind = 'input';
    else if (tag === 'a' || role === 'link') kind = 'link';
    else if (isInteractive) kind = 'button';
    else if (PRICE.test(text)) kind = 'price';
    const form = el.closest('form');
    const r = el.getBoundingClientRect();
    out.push({
      selector: selectorFor(el), tag, role, kind, text: text.slice(0, 300),
      href: navTarget(el),
      input_type: field ? (el.type || tag) : null,
      name: el.getAttribute('name'),
      required: field ? !!el.required : null,
      options: tag === 'select' ? Array.from(el.options).map(o => norm(o.text)).slice(0, 20) : null,
      form: form ? selectorFor(form) : null,
      is_submit: !!form && (tag === 'button' ? (el.type || 'submit') === 'submit' : (tag === 'input' && el.type === 'submit')),
      bbox: [Math.round(r.left + scrollX), Math.round(r.top + scrollY), Math.round(r.width), Math.round(r.height)],
    });
  }
  return { url: location.href, title: document.title, elements: out };
}
"""

# After a click that did not navigate, what did the page show? (toast, dialog, alert)
FEEDBACK_JS = r"""
() => {
""" + HELPERS_JS + r"""
  const el = feedbackEl();
  return el ? norm(el.innerText).slice(0, 300) : null;
}
"""

# Recorder: what each cited element says right now, and where it is in the viewport.
ELEMENTS_INFO_JS = r"""
(selectors) => {
""" + HELPERS_JS + r"""
  return selectors.map(sel => {
    let el = null;
    try { el = document.querySelector(sel); } catch (e) { return null; }
    if (!el) return null;
    return { text: textFor(el).slice(0, 300), rect: rectOf(el), visible: visible(el) };
  });
}
"""

FEEDBACK_INFO_JS = r"""
() => {
""" + HELPERS_JS + r"""
  const el = feedbackEl();
  return el ? { text: norm(el.innerText).slice(0, 300), rect: rectOf(el) } : null;
}
"""

# Clicking these on a real product could do real damage, so the Explorer never does.
DANGEROUS = re.compile(
    r"\b(delete|remove|destroy|erase|pay|buy|purchase|checkout|order|subscribe|unsubscribe|"
    r"log ?out|sign ?out|deactivate|cancel|transfer|send money|withdraw|refund)\b", re.I)


def is_dangerous(text: str | None) -> bool:
    return bool(text and DANGEROUS.search(text))


def synthetic_value(field: dict) -> str | None:
    """Obviously-fake values for exploring forms on a product you own."""
    t = (field.get("input_type") or "text").lower()
    hint = f"{field.get('name') or ''} {field.get('text') or ''}".lower()
    if t == "password":
        return None
    if t == "email" or "email" in hint:
        return "demo@example.com"
    if t == "number" or "amount" in hint or "price" in hint:
        return "120"
    if t == "date" or "date" in hint:
        return (_dt.date.today() + _dt.timedelta(days=14)).isoformat()
    if t == "tel" or "phone" in hint:
        return "5550100"
    if t == "url" or "website" in hint:
        return "https://example.com"
    if "company" in hint or "client" in hint or "organization" in hint:
        return "Acme Corp"
    if "name" in hint:
        return "Demo User"
    return "Sample text"
