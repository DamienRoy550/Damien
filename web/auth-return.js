/* Hand-off page for the OIDC callback: stores the bearer token, then returns to
 * the app. Kept in its own file because the CSP allows script-src 'self' only. */
try {
  const pre = document.getElementById("token");
  const payload = JSON.parse(pre.textContent);
  if (payload && payload.session && payload.session.token) {
    localStorage.setItem("jarvis.token", payload.session.token);
    location.replace("/web/index.html");
  }
} catch (err) {
  const box = document.querySelector(".dev-card");
  if (box) box.insertAdjacentHTML("beforeend", "<p>Could not complete the hand-off automatically.</p>");
}
