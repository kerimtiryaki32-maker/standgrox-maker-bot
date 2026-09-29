// Run locally. Never publish the generated signing key.
const { randomBytes } = require("node:crypto");
console.log("STANDX_SIGN_KEY_HEX=" + randomBytes(32).toString("hex"));
