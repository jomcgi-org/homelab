export const discoveryV2Beta1ContentType =
  "application/json;as=APIGroupDiscoveryList;v=v2beta1;g=apidiscovery.k8s.io";
export const discoveryV2ContentType =
  "application/json;as=APIGroupDiscoveryList;v=v2;g=apidiscovery.k8s.io";

export interface V1GroupVersionKind {
  group: string;
  version: string;
  kind: string;
}

export enum V2ResourceScope {
  Cluster = "Cluster",
  Namespaced = "Namespaced",
}

export interface V2APISubresourceDiscovery {
  subresource: string;
  responseKind: V1GroupVersionKind;
  acceptedTypes?: Array<V1GroupVersionKind>;
  verbs: Array<string>;
}

export interface V2APIResourceDiscovery {
  resource: string;
  responseKind: V1GroupVersionKind;
  scope: V2ResourceScope;
  singularResource: string;
  verbs: Array<string>;
  shortNames?: Array<string>;
  categories?: Array<string>;
  subresouces?: Array<V2APISubresourceDiscovery>;
}

export enum V2DiscoveryFreshness {
  Current = "Current",
  Stale = "Stale",
}

export interface V2APIVersionDiscovery {
  version: string;
  resources: Array<V2APIResourceDiscovery>;
  freshness: V2DiscoveryFreshness;
}

export interface V2APIGroupDiscovery {
  metadata: { name?: string };
  versions: V2APIVersionDiscovery[];
}
