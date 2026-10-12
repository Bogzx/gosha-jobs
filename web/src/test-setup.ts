import '@testing-library/jest-dom/vitest'

// jsdom has no matchMedia: default to a phone-width viewport. Tests that
// need to resize (Feed.test.tsx) install their own.
if (!window.matchMedia) {
  window.matchMedia = (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => false,
  })
}
