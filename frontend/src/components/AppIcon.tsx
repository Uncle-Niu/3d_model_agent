/**
 * AppIcon — the agent's brand mark. An isometric CAD-style wireframe cube
 * with a highlighted origin vertex (the parametric anchor point), used
 * consistently in the header and the chat welcome screen.
 */

interface AppIconProps {
  size?: number;
  className?: string;
}

export default function AppIcon({ size = 24, className }: AppIconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      <path d="M12 2 L20 7 L20 17 L12 22 L4 17 L4 7 Z" />
      <path d="M12 22 L12 12 L20 7 M12 12 L4 7" />
      <circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none" />
    </svg>
  );
}
