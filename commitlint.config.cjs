module.exports = {
  extends: ["@commitlint/config-conventional"],
  ignores: [
    (message) => message.startsWith("Merge pull request "),
    (message) => message.startsWith("Merge branch "),
    (message) => message.startsWith("Merge remote-tracking branch "),
  ],
}

