// Relay GlobalID is base64("TypeName:rawId"). Decode to the raw DB id when
// we need to hand the value to a REST endpoint or a non-Relay route param.
export function decodeGlobalId(gid: string): string {
  try {
    const decoded = atob(gid);
    const colon = decoded.indexOf(':');
    return colon >= 0 ? decoded.slice(colon + 1) : gid;
  } catch {
    return gid;
  }
}

export function encodeGlobalId(typeName: string, rawId: string): string {
  return btoa(`${typeName}:${rawId}`);
}
