// Boss-facing display-language preference for conversation panes. Defaults to
// Chinese (the boss reads Chinese, not English) and is persisted so the choice
// survives reloads and is shared by the inbox reading pane and the full
// conversation page.
const KEY = "ui.lang.show_cn";

export function loadShowCn(): boolean {
  const stored = localStorage.getItem(KEY);
  return stored === null ? true : stored === "1";
}

export function saveShowCn(showCn: boolean): void {
  localStorage.setItem(KEY, showCn ? "1" : "0");
}
