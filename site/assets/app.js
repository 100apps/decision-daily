/* Progressive enhancement only: reading and archive navigation work without JS. */
(() => {
  "use strict";
  document.querySelectorAll("[data-print]").forEach(button => {
    button.addEventListener("click", () => window.print());
  });

  const freshness = document.getElementById("freshness-note");
  const checkFreshness = () => {
    if (!freshness) return;
    const parts = new Intl.DateTimeFormat("en-GB", {
      timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", hourCycle: "h23"
    }).formatToParts(new Date());
    const values = Object.fromEntries(parts.map(part => [part.type, part.value]));
    const today = `${values.year}-${values.month}-${values.day}`;
    freshness.hidden = !(Number(values.hour) >= 9 && freshness.dataset.latestDate < today);
  };
  checkFreshness();
  if (freshness) {
    window.setInterval(checkFreshness, 60000);
    document.addEventListener("visibilitychange", checkFreshness);
  }

  const input = document.getElementById("archive-search");
  if (input) {
    const cards = [...document.querySelectorAll(".issue-card[data-search]")];
    const sections = [...document.querySelectorAll(".archive-month")];
    const status = document.getElementById("search-status");
    const empty = document.getElementById("search-empty");
    input.addEventListener("input", () => {
      const terms = input.value.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
      let count = 0;
      cards.forEach(card => {
        const text = card.dataset.search.toLocaleLowerCase();
        card.hidden = !terms.every(term => text.includes(term));
        if (!card.hidden) count += 1;
      });
      sections.forEach(section => {
        section.hidden = ![...section.querySelectorAll(".issue-card")].some(card => !card.hidden);
      });
      empty.hidden = count > 0;
      status.textContent = terms.length ? `找到 ${count} 期日报` : "";
    });
  }

  const toc = document.querySelector(".toc-panel");
  if (toc && window.matchMedia("(max-width: 720px)").matches) {
    toc.open = false;
    toc.querySelectorAll("a").forEach(link => {
      link.addEventListener("click", () => { toc.open = false; });
    });
  }
})();
