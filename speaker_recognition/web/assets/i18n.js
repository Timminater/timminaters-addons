"use strict";

(() => {
  const supported = ["nl", "en"];
  const storageKey = "speaker-recognition-language";
  const basePath = document.querySelector('meta[name="ingress-base"]').content;
  const originalText = new WeakMap();
  const originalAttributes = new WeakMap();
  let catalog = { locale: "nl-NL", translations: {}, patterns: [] };

  const normalize = (value) => {
    const language = String(value || "").toLowerCase().split(/[-_]/)[0];
    return supported.includes(language) ? language : null;
  };

  function detectLanguage() {
    try {
      const stored = normalize(localStorage.getItem(storageKey));
      if (stored) return stored;
    } catch (_) { /* Storage can be unavailable in hardened browsers. */ }
    for (const value of navigator.languages || [navigator.language]) {
      const language = normalize(value);
      if (language) return language;
    }
    return "nl";
  }

  function translate(value) {
    if (value == null || typeof value !== "string") return value;
    if (catalog.translations[value] != null) return catalog.translations[value];
    for (const pattern of catalog.patterns || []) {
      const expression = new RegExp(pattern.match, pattern.flags || "");
      if (expression.test(value)) return value.replace(expression, pattern.replace);
    }
    return value;
  }

  function applyTranslations() {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      if (node.parentElement?.closest("script, style")) continue;
      if (!originalText.has(node)) originalText.set(node, node.nodeValue);
      const source = originalText.get(node);
      const match = source.match(/^(\s*)([\s\S]*?)(\s*)$/);
      node.nodeValue = `${match[1]}${translate(match[2])}${match[3]}`;
    }
    document.querySelectorAll("[aria-label], [placeholder], [title]").forEach((element) => {
      let values = originalAttributes.get(element);
      if (!values) {
        values = Object.fromEntries(["aria-label", "placeholder", "title"]
          .filter((name) => element.hasAttribute(name))
          .map((name) => [name, element.getAttribute(name)]));
        originalAttributes.set(element, values);
      }
      Object.entries(values).forEach(([name, source]) => element.setAttribute(name, translate(source)));
    });
  }

  async function initialize() {
    const language = detectLanguage();
    try {
      const response = await fetch(`${basePath}assets/languages/${language}.json`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      catalog = await response.json();
    } catch (error) {
      console.warn(`Could not load language '${language}', using Dutch source text.`, error);
    }
    document.documentElement.lang = language;
    window.currentLanguage = language;
    applyTranslations();
    const select = document.querySelector("#language-select");
    if (select) {
      select.value = language;
      select.addEventListener("change", () => {
        try { localStorage.setItem(storageKey, select.value); } catch (_) { /* no-op */ }
        location.reload();
      });
    }
  }

  window.tr = translate;
  window.i18nReady = initialize();
})();
