/*
  Shared compact generation-credit status for the jobs, cover-letter, and ATS
  pages. Visible text is intentionally short; title/aria-label carry the full
  meaning for assistive tech and hover.
*/
(function () {
  "use strict";

  function i18n(key, fallback, vars) {
    var value = fallback;
    if (window.OnePagerI18n) {
      var translated = window.OnePagerI18n.t(key, vars || {});
      if (translated && translated !== key) value = translated;
    }
    if (vars) {
      Object.keys(vars).forEach(function (name) {
        value = value.replace(new RegExp("\\{" + name + "\\}", "g"), vars[name]);
      });
    }
    return value;
  }

  function stateFromStatus(status) {
    var provider = status && (status.provider_label || status.provider);
    var model = status && status.model;
    var providerText = provider ? String(provider).replace(/\s*\(.+\)\s*$/, "") : "";
    var modelText = model ? String(model) : "";
    var providerLabel = providerText && modelText ? providerText + " · " + modelText : providerText;
    if (!status || status.unavailable) {
      return {
        state: "unavailable",
        text: providerText || i18n("credit.unavailable.short", "AI"),
        label: providerLabel || i18n("credit.unavailable.full", "Credit status unavailable"),
      };
    }
    if (status.loading) {
      return {
        state: "loading",
        text: i18n("credit.loading.short", "..."),
        label: i18n("credit.loading.full", "Checking generation credit status"),
      };
    }
    if (status.connected) {
      return {
        state: "connected",
        text: providerText || i18n("credit.connected.short", "Key"),
        label: providerLabel || i18n("credit.connected.full", "AI key connected"),
      };
    }
    var n = Math.max(0, Number(status.managed_credits_remaining || 0));
    if (n === 0) {
      return {
        state: "zero",
        text: providerText || i18n("credit.zero.short", "0 left"),
        label: providerLabel
          ? providerLabel + " unavailable. Add that provider key in Account."
          : i18n("credit.zero.full", "No free generations left. Add an AI key to continue."),
      };
    }
    return {
      state: "remaining",
      text: providerText || i18n("credit.remaining.short", "{n} left", { n: n }),
      label: (providerLabel ? providerLabel + ". " : "") + i18n(
        n === 1 ? "credit.remaining.full.one" : "credit.remaining.full.many",
        n === 1 ? "1 free generation left" : "{n} free generations left",
        { n: n }
      ),
    };
  }

  function render(el, status) {
    if (!el) return;
    var display = stateFromStatus(status);
    el.hidden = false;
    el.textContent = display.text;
    el.title = display.label;
    el.setAttribute("aria-label", display.label);
    el.setAttribute("data-credit-state", display.state);
  }

  window.OnePagerCredit = { render: render, stateFromStatus: stateFromStatus };
})();
