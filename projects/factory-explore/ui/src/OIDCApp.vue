<script setup lang="ts">
import App from "./App.vue";
import { ref, onMounted } from "vue";
import { userManager, getIdToken } from "./apiConfig";

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
      window.location.replace("/");
    } else if (path === "/oidc/logout") {
      await userManager.removeUser();
      message.value = "Logged out";
    } else {
      await getIdToken();
      oidcCompleted.value = true;
    }
  } catch (error) {
    message.value = String(error);
  }
});
</script>

<template>
  <App v-if="oidcCompleted" />
  <div v-else>
    {{ message }}
  </div>
</template>
