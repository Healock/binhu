import { useEffect, useMemo, type CSSProperties } from 'react'
import { createPortal } from 'react-dom'

interface CompletionCelebrationProps {
  open: boolean
  onClose: () => void
}

type Particle = {
  angle: number
  distance: number
  delay: number
  hue: number
  size: number
}

const BURSTS = [
  { x: '16%', y: '24%', scale: 0.82 },
  { x: '38%', y: '16%', scale: 1 },
  { x: '61%', y: '27%', scale: 0.9 },
  { x: '82%', y: '18%', scale: 0.78 },
  { x: '28%', y: '58%', scale: 0.7 },
  { x: '72%', y: '62%', scale: 0.86 },
] as const

function makeParticles(seed: number): Particle[] {
  return Array.from({ length: 18 }, (_, index) => ({
    angle: index * 20 + (seed % 9),
    distance: 38 + ((index * 17 + seed) % 42),
    delay: ((index * 13 + seed) % 12) / 100,
    hue: (seed * 17 + index * 23) % 360,
    size: 3 + ((index + seed) % 3),
  }))
}

export default function CompletionCelebration({ open, onClose }: CompletionCelebrationProps) {
  const bursts = useMemo(() => BURSTS.map((burst, index) => ({ ...burst, particles: makeParticles(index + 11) })), [])

  useEffect(() => {
    if (!open) return
    const timer = window.setTimeout(onClose, 3600)
    return () => window.clearTimeout(timer)
  }, [onClose, open])

  if (!open || typeof document === 'undefined') return null

  return createPortal(
    <div className="completion-celebration" role="status" aria-live="polite" aria-label="完成率已达到百分之百">
      <div className="completion-celebration__message">全部完成！</div>
      {bursts.map((burst, burstIndex) => (
        <div
          className="completion-celebration__burst"
          key={`${burst.x}-${burst.y}`}
          style={{ left: burst.x, top: burst.y, '--burst-scale': burst.scale } as CSSProperties}
        >
          {burst.particles.map((particle, particleIndex) => (
            <i
              className="completion-celebration__particle"
              key={`${burstIndex}-${particleIndex}`}
              style={{
                '--particle-angle': `${particle.angle}deg`,
                '--particle-distance': `${particle.distance}px`,
                '--particle-delay': `${particle.delay}s`,
                '--particle-hue': particle.hue,
                '--particle-size': `${particle.size}px`,
              } as CSSProperties}
            />
          ))}
        </div>
      ))}
    </div>,
    document.body,
  )
}
