// Dynamic wrapper around app.json - the only reason this file exists is to
// let `android.googleServicesFile` resolve to an EAS-supplied secret file
// variable (GOOGLE_SERVICES_JSON) at build time instead of the local,
// git-ignored copy on disk, since a plain app.json cannot reference
// `process.env` at all. Everything else stays exactly what app.json says -
// this file must never duplicate/redeclare app.json's config.
const config = require('./app.json');

config.expo.android.googleServicesFile =
  process.env.GOOGLE_SERVICES_JSON ?? config.expo.android.googleServicesFile;

module.exports = config;
