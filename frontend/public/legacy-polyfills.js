(function installLegacyWebViewPolyfills() {
  if (typeof Object.hasOwn !== 'function') {
    Object.defineProperty(Object, 'hasOwn', {
      configurable: true,
      writable: true,
      value: function hasOwn(object, property) {
        return Object.prototype.hasOwnProperty.call(object, property)
      },
    })
  }
})()
