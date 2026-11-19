<script setup lang="ts">
import App from "./App.vue";
import { ref, onMounted, onUnmounted } from "vue";
import {
  userManager,
  getIdToken,
  completeSignin,
  updateClock,
  nextWindow,
  restart,
} from "./apiConfig";

const oidcCompleted = ref(false);
const message = ref("Handling OIDC authentication...");

onMounted(async () => {
  try {
    const base = new URL(
      window.__base_url,
      window.location.origin,
    ).pathname.replace(/\/$/, "");
    const path = window.location.pathname.replace(base, "");
    if (path === "/oidc/callback") {
      await userManager.signinRedirectCallback();
      completeSignin();
      window.location.replace("/");
    } else if (path === "/oidc/logout") {
      await userManager.removeUser();
      message.value = "Logged out";
    } else {
      await getIdToken({ initialVisit: true });
      // A suspended tab can resume before its overdue timeout fires.
      document.addEventListener("visibilitychange", updateClock);
      window.addEventListener("pageshow", updateClock);
      oidcCompleted.value = true;
    }
  } catch (error) {
    message.value = String(error);
  }
});
onUnmounted(() => {
  document.removeEventListener("visibilitychange", updateClock);
  window.removeEventListener("pageshow", updateClock);
});
</script>

<template>
  <main v-if="nextWindow !== null">
    <h1>Time’s up</h1>
    <p v-if="nextWindow">
      Your next window opens at {{ new Date(nextWindow).toLocaleString() }}.
    </p>
    <p v-else>Your explorer session has expired.</p>
    <button type="button" @click="restart">Sign in again</button>
  </main>
  <App v-else-if="oidcCompleted" />
  <div v-else>
    {{ message }}
  </div>
</template>
