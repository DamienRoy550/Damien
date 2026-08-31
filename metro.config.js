const { getDefaultConfig } = require('expo/metro-config');

/** @type {import('expo/metro-config').MetroConfig} */
const config = getDefaultConfig(__dirname);

// Allow importing .md docs and .gguf model manifests if we ever bundle them
config.resolver.assetExts.push('md');

module.exports = config;
