const appDb = db.getSiblingDB("propcluev1");
const readOnlyUser = process.env.PROPCLUE_MONGO_READONLY_USER;
const readOnlyPassword = process.env.PROPCLUE_MONGO_READONLY_PASSWORD;

if (!readOnlyUser || !readOnlyPassword) {
  throw new Error("MongoDB read-only credentials are required.");
}

appDb.createUser({
  user: readOnlyUser,
  pwd: readOnlyPassword,
  roles: [{ role: "read", db: "propcluev1" }],
});
