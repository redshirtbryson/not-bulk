// Collection explorer is fully server-rendered: sort/filter are query-param links and a
// GET form, so no JavaScript is required for core behavior (CSP-safe, no inline handlers).
// This file exists as the mount point for future progressive enhancement (e.g. live filter
// preview) and to satisfy the <script src="/js/collection.js"> reference in collection.njk.
"use strict";
