<script setup lang="ts">
import { ref, watch, onMounted } from "vue";
import {
  VApp,
  VAppBar,
  VAppBarTitle,
  VBtn,
  VContainer,
  VDataTable,
  VMain,
  VSelect,
  VTextField,
} from "vuetify/components";
import { read } from "./apiConfig";

import {
  discoveryV2ContentType,
  discoveryV2Beta1ContentType,
  type V2APIGroupDiscovery,
} from "./discovery";

interface Resource {
  title: string;
  prefix: string;
  plural: string;
  namespaced: boolean;
}
const resources = ref<Resource[]>([]);
const resource = ref<Resource>();
// Explorers may read only the namespaces bound to them, never "default", so
// no namespaced request starts until the user names one.
const namespace = ref("");
const rows = ref<{ cells: unknown[] }[]>([]);
const columns = ref<{ title: string; key: string }[]>([]);
const error = ref("");
const loading = ref(false);
const refresh = ref(0);

onMounted(async () => {
  try {
    const groups = await Promise.all(
      ["/api", "/apis"].map(async (path) => {
        const result = await read(
          path,
          discoveryV2ContentType + "," + discoveryV2Beta1ContentType,
        );
        if (result.kind !== "APIGroupDiscoveryList")
          throw new Error("Aggregated API discovery is unavailable");
        return result.items as V2APIGroupDiscovery[];
      }),
    );
    for (const group of groups.flat()) {
      const name = group.metadata.name;
      const version = group.versions.find((v) => v.freshness === "Current");
      if (!version) continue;
      for (const api of version.resources ?? []) {
        if (!api.verbs.includes("list")) continue;
        resources.value.push({
          title: name ? api.resource + "." + name : api.resource,
          prefix: name
            ? `/apis/${name}/${version.version}`
            : `/api/${version.version}`,
          plural: api.resource,
          namespaced: api.scope === "Namespaced",
        });
      }
    }
    resources.value.sort((a, b) => a.title.localeCompare(b.title));
    resource.value =
      resources.value.find((r) => r.title === "pods") ?? resources.value[0];
  } catch (e) {
    error.value = String(e);
  }
});
watch(
  [resource, namespace, refresh],
  async ([kind, ns], _, cleanup) => {
    const controller = new AbortController();
    cleanup(() => controller.abort());
    rows.value = [];
    error.value = "";
    loading.value = false;
    if (
      !kind ||
      (kind.namespaced && !/^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/.test(ns))
    )
      return;
    loading.value = true;
    try {
      const scope = kind.namespaced ? "/namespaces/" + ns : "";
      const table = await read(
        kind.prefix + scope + "/" + kind.plural,
        "application/json;as=Table;g=meta.k8s.io;v=v1",
        controller.signal,
      );
      if (controller.signal.aborted) return;
      columns.value = table.columnDefinitions.map(
        (column: { name: string }, index: number) => ({
          title: column.name,
          key: "cells." + index,
        }),
      );
      rows.value = table.rows ?? [];
    } catch (e) {
      if (!controller.signal.aborted) error.value = String(e);
    } finally {
      if (!controller.signal.aborted) loading.value = false;
    }
  },
  { immediate: true },
);
</script>

<template>
  <VApp>
    <VAppBar
      ><VAppBarTitle>Sparkles · Factory Explorer</VAppBarTitle
      ><VBtn href="/oidc/logout">Log out</VBtn></VAppBar
    >
    <VMain
      ><VContainer>
        <VTextField
          v-if="resource?.namespaced"
          label="Namespace"
          hint="Enter a namespace you may read"
          persistent-hint
          v-model="namespace"
        />
        <VSelect
          label="Resource"
          :items="resources"
          return-object
          v-model="resource"
        />
        <VBtn @click="refresh++">Refresh</VBtn>
        <p v-if="error" role="alert">{{ error }}</p>
        <VDataTable
          :headers="columns"
          :items="rows"
          :loading="loading"
        /> </VContainer
    ></VMain>
  </VApp>
</template>
