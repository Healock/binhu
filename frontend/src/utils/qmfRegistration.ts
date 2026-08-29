export function normalizeQmfCommunityCodeInput(value: string): string {
  return value.toUpperCase().replace(/[^0-9A-Z]/g, '').slice(0, 10)
}
