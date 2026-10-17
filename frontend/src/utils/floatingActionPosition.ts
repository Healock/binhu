export interface FloatingActionPosition { x: number; y: number }

export function clampFloatingActionPosition(
  position: FloatingActionPosition,
  width: number,
  height: number,
  bottomInset: number,
): FloatingActionPosition {
  return {
    x: Math.max(12, Math.min(position.x, width - 64)),
    y: Math.max(72, Math.min(position.y, height - 52 - bottomInset)),
  }
}
