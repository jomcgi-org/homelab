import "@mdi/font/css/materialdesignicons.css";

import { createApp } from "vue";
import { createVuetify } from "vuetify";
import "vuetify/styles";
import App from "./OIDCApp.vue";
const app = createApp(App);

app.use(
  createVuetify({
    theme: {
      defaultTheme: "dark",
    },
    defaults: {
      VDataTable: {
        hideDefaultFooter: true,
        hover: true,
        itemsPerPage: -1,
      },
    },
  }),
);

app.mount("#app");
