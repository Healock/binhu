export function resolveApiAssetUrl(
  assetUrl: string | null | undefined,
  configuredApiBaseUrl: string,
): string | null {
  if (!assetUrl) return null
  if (/^[a-z][a-z\d+.-]*:/i.test(assetUrl) || assetUrl.startsWith('//')) {
    return assetUrl
  }

  const apiBaseUrl = configuredApiBaseUrl.replace(/\/+$/, '')
  if (!apiBaseUrl) return assetUrl
  if (assetUrl === '/api') return apiBaseUrl
  if (assetUrl.startsWith('/api/')) {
    return `${apiBaseUrl}${assetUrl.slice(4)}`
  }
  return `${apiBaseUrl}/${assetUrl.replace(/^\/+/, '')}`
}
