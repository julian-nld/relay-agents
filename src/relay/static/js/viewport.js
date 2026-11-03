// Phones: the on-screen keyboard. iOS (and Android Chrome unless asked: index.html's
// interactive-widget) shrinks only the visual viewport for it, so a layout as tall as the screen
// keeps the message box under the keyboard. The layout follows the visual viewport's height instead
// (--app-height, css/app.css), and stays at its top: iOS scrolls the page to show a field, which a
// layout that fits what's visible doesn't need. Not while zoomed in: then it's the user panning.

export function followVisualViewport(win = window, root = document.documentElement) {
  const view = win.visualViewport;
  if (!view) return () => {};
  const apply = () => {
    if (view.scale > 1.01) return;
    root.style.setProperty("--app-height", `${Math.round(view.height)}px`);
    if (win.scrollY || view.offsetTop) win.scrollTo(0, 0);
  };
  view.addEventListener("resize", apply);
  view.addEventListener("scroll", apply);
  apply();
  return () => {
    view.removeEventListener("resize", apply);
    view.removeEventListener("scroll", apply);
  };
}
