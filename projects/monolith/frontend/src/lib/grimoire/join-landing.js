// Standalone by design: the public join route cannot fetch authenticated JS,
// fonts or telemetry. Keep the token in this function's temporary scope only.
export async function startJoinLanding() {
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) window.location.reload();
  });
  const status = document.getElementById("status");
  const details = document.getElementById("invitation");
  const actions = document.getElementById("actions");
  const enrollment = document.getElementById("enrollment");
  const token = window.location.hash.slice(1);
  // Clear before the first request, including invalid tokens and query strings.
  window.history.replaceState(null, "", window.location.pathname);
  details.hidden = true;
  actions.hidden = true;
  enrollment.hidden = true;
  try {
    const response = await fetch("/grimoire/join", {
      method: "POST",
      credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(
        token ? { action: "capture", token } : { action: "inspect" },
      ),
    });
    const result = await response.json();
    if (!response.ok)
      throw new Error(
        result.error ||
          "This invitation could not be opened. Please try the original link again.",
      );
    document.getElementById("enrollment-invitation").value = result.id;
    document.getElementById("campaign").textContent = result.campaign_name;
    document.getElementById("recipient").textContent = result.invitee_email;
    document.getElementById("expires").textContent = new Date(
      result.expires_at,
    ).toLocaleString();
    document.getElementById("link-status").textContent = result.status;
    details.hidden = false;
    if (result.status === "pending") {
      status.textContent =
        "This invitation is for one player. Sign in with the invited email to review and accept it.";
      actions.hidden = false;
      enrollment.hidden = !result.can_enroll;
    } else if (result.status === "accepted") {
      status.textContent =
        "This invitation has already been accepted. Sign in to open your campaigns.";
      const signIn = document.getElementById("sign-in");
      signIn.href = "/grimoire";
      signIn.textContent = "Open your campaigns";
      actions.hidden = false;
    } else {
      status.textContent =
        "This invitation is no longer available. Ask the campaign owner for a new link.";
    }
  } catch (error) {
    status.textContent =
      error.message ||
      "This invitation could not be opened. Please try the original link again.";
    status.setAttribute("role", "alert");
  }
  for (const form of document.querySelectorAll("form")) {
    let pending = false;
    form.addEventListener("submit", (event) => {
      if (pending) {
        event.preventDefault();
        return;
      }
      pending = true;
      for (const button of form.querySelectorAll("button"))
        button.disabled = true;
    });
  }
}
