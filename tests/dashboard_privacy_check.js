// Execute the generated inline script against an offline DOM with request spies.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { URL } = require("node:url");

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const requests = [];

class Element {
  constructor(tag, options = {}) {
    this.tag = tag;
    this.dataset = options.dataset || {};
    this.value = options.value || "";
    this.hidden = !!options.hidden;
    this.disabled = !!options.disabled;
    this.style = {};
    this.children = [];
    this.handlers = {};
    this.textContent = "";
  }
  addEventListener(name, callback) { this.handlers[name] = callback; }
  append(...children) { this.children.push(...children); }
  setAttribute(name, value) { this[name] = value; }
  set src(value) {
    this._src = value;
    if (this.tag === "img" || this.tag === "script") requests.push([this.tag, value]);
  }
  set href(value) {
    this._href = value;
    if (this.tag === "link") requests.push([this.tag, value]);
  }
}

const ids = {
  "#theme": new Element("button"),
  "#status": new Element("select", { value: "all" }),
  "#price": new Element("input"),
  "#commute": new Element("input"),
  "#sort": new Element("select", { value: "price" }),
  "#flats": new Element("div"),
  "#map-fallback": new Element("svg"),
  "#load-external": new Element("button", { disabled: true }),
  "#external-heading": new Element("h2"),
  "#external-status": new Element("p"),
  "#photo-hosts": new Element("span"),
};
const images = input.images.map(url => new Element("img", { dataset: { remoteSrc: url }, hidden: true }));
const cards = images.map(() => new Element("article", {
  dataset: { status: "available", price: "1500", commute: "25" },
}));
const document = {
  documentElement: { dataset: {} },
  head: new Element("head"),
  body: new Element("body"),
  querySelector: selector => ids[selector],
  querySelectorAll: selector => ({
    ".flat": cards,
    ".filters input,.filters select": [],
    "img[data-remote-src]": images,
  })[selector] || [],
  createElement: tag => new Element(tag),
  createElementNS: (_, tag) => new Element(tag),
  createTextNode: text => ({ textContent: text }),
};
const map = { setView() { return this; }, fitBounds() { return this; } };
const L = {
  map: () => map,
  tileLayer: url => ({
    addTo() { requests.push(["tile", url]); return this; },
    on() { return this; },
  }),
  circleMarker: () => ({ addTo() { return this; }, bindPopup() {} }),
  latLngBounds: () => ({ pad() { return this; } }),
};

vm.runInNewContext(input.script, {
  document, URL, L, matchMedia: () => ({ matches: false }),
  fetch: url => { requests.push(["fetch", url]); throw new Error("Network disabled"); },
});

assert.deepEqual(requests, [], "opening the page must not request external content");
assert.equal(ids["#load-external"].disabled, false);
assert.equal(ids["#photo-hosts"].textContent, "example.test, images.other.test");
assert.ok(ids["#map-fallback"].children.length > 0, "the local map is populated without opt-in");
assert.ok(images.every(image => image.hidden), "photos remain hidden without opt-in");

ids["#load-external"].handlers.click();
assert.equal(ids["#load-external"].disabled, true);
assert.equal(ids["#external-heading"].textContent, "External content enabled");
assert.ok(images.every(image => !image.hidden));
const css = document.head.children.find(element => element.tag === "link");
const script = document.body.children.find(element => element.tag === "script");
assert.ok(css && script, "opt-in loads the Leaflet assets");
css.onload();
script.onload();
assert.deepEqual(requests.map(([kind]) => kind), ["img", "img", "link", "script", "tile"]);
assert.ok(requests.some(([kind, url]) => kind === "tile" && url.includes("tile.openstreetmap.org")));
ids["#load-external"].handlers.click();
assert.equal(requests.length, 5, "a second click must not repeat the requests");
