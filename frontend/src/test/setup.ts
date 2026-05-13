import '@testing-library/jest-dom';

// jsdom does not implement scrollIntoView — polyfill for tests
window.HTMLElement.prototype.scrollIntoView = function () {};

// jsdom does not implement ResizeObserver
globalThis.ResizeObserver = class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
};
