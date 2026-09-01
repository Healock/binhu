export interface MobileTaskFilterValueOption {
  value: string
}

export function retainAvailableMobileTaskFilters(
  selected: string[],
  options: MobileTaskFilterValueOption[],
) {
  const available = new Set(options.map(option => option.value))
  return selected.filter(value => available.has(value))
}
