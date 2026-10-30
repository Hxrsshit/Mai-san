/** Keyboard behaviour for a tab list (the WAI-ARIA tabs pattern). Pure. */

/**
 * The tab to focus after `key`, or null if the key is not a tab-navigation key.
 * Arrow keys wrap, Home and End jump to the ends.
 */
export function nextTabIndex(key: string, current: number, count: number): number | null {
  if (count <= 0) return null;
  switch (key) {
    case "ArrowRight":
    case "ArrowDown":
      return (current + 1) % count;
    case "ArrowLeft":
    case "ArrowUp":
      return (current - 1 + count) % count;
    case "Home":
      return 0;
    case "End":
      return count - 1;
    default:
      return null;
  }
}
