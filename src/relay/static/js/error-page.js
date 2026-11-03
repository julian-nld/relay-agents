// Relay's error page (web.error_page): the theme chosen in this browser, and back to where you came from.
import "./theme.js";

const back = document.querySelector("[data-back]");
if (back && history.length > 1) {
  back.hidden = false;
  back.addEventListener("click", () => history.back());
}
