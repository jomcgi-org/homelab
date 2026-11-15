{{/* Fixed identities from the factory and platform-factory policies. Change the
catalogue in review when a peer moves; consumers cannot override selectors. */}}
{{- define "factory-library.peers" -}}
edge:
  k8s:io.kubernetes.pod.namespace: edge
  gateway.envoyproxy.io/owning-gateway-name: edge
  gateway.envoyproxy.io/owning-gateway-namespace: edge
garage:
  k8s:io.kubernetes.pod.namespace: garage
  app.kubernetes.io/name: garage
itsaplan-api:
  k8s:io.kubernetes.pod.namespace: factory
  app.kubernetes.io/name: factory-itsaplan
  app.kubernetes.io/instance: factory
  app.kubernetes.io/component: api
itsaplan-web:
  k8s:io.kubernetes.pod.namespace: factory
  app.kubernetes.io/name: factory-itsaplan
  app.kubernetes.io/instance: factory
  app.kubernetes.io/component: web
itsaplan-worker:
  k8s:io.kubernetes.pod.namespace: factory
  app.kubernetes.io/name: factory-itsaplan
  app.kubernetes.io/instance: factory
  app.kubernetes.io/component: worker
kagent-controller:
  k8s:io.kubernetes.pod.namespace: kagent
  app.kubernetes.io/name: kagent
  app.kubernetes.io/instance: factory
  app.kubernetes.io/component: controller
kagent-ui:
  k8s:io.kubernetes.pod.namespace: kagent
  app.kubernetes.io/name: kagent
  app.kubernetes.io/instance: factory
  app.kubernetes.io/component: ui
broker:
  k8s:io.kubernetes.pod.namespace: factory
  app.kubernetes.io/name: factory-broker
  app.kubernetes.io/instance: factory
connector:
  k8s:io.kubernetes.pod.namespace: factory
  app.kubernetes.io/name: factory-connector
reviewer:
  k8s:io.kubernetes.pod.namespace: factory
  app.kubernetes.io/name: factory-reviewer
  app.kubernetes.io/instance: factory
factory-db:
  k8s:io.kubernetes.pod.namespace: factory-db
  cnpg.io/cluster: factory-db
kagent-postgres:
  k8s:io.kubernetes.pod.namespace: kagent-pg
  cnpg.io/cluster: kagent-pg
substrate-api:
  k8s:io.kubernetes.pod.namespace: ate-system
  app: ate-api-server
substrate-idp:
  k8s:io.kubernetes.pod.namespace: ate-system
  app: ate-idp-server
substrate-router:
  k8s:io.kubernetes.pod.namespace: ate-system
  app: atenet-router
substrate-egress:
  k8s:io.kubernetes.pod.namespace: ate-system
  app: atenet-egress
{{- end -}}
