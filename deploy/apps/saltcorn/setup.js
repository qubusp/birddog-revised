// Builds the Saltcorn "Curator" app: remote tables on the Birddog database
// (via the postgres-tables plugin, as birddog_ui), views, a search page and
// a menu entry. Idempotent: re-running updates everything in place.
//
// Needs migration deploy/db/migrations/009_saltcorn_curator.sql. Run it
// inside the saltcorn container (see deploy/apps/README.md, "Saltcorn Curator app"):
//   docker compose cp deploy/apps/saltcorn/setup.js saltcorn:/tmp/setup.js
//   docker compose exec -T -e NODE_PATH=/usr/local/lib/node_modules/@saltcorn/cli/node_modules \
//     saltcorn node /tmp/setup.js
//   docker compose restart saltcorn   # the web process caches views/tables
//
// The database password is never stored: with password "" the plugin reads
// SC_EXTPG_PASS_birddog from the container environment.

const STAFF = 40; // Saltcorn role ids: 1 admin, 40 staff, 80 user, 100 public

const CONN = {
  host: "postgres",
  port: 5432,
  user: "birddog_ui",
  password: "",
  database: "birddog",
  ssl: false,
};

// -------------------------------------------------------------- tables
const S = (name, label) => ({ name, label: label || name, type: "String" });
const I = (name, label) => ({ name, label: label || name, type: "Integer" });
const D = (name, label) => ({ name, label: label || name, type: "Date" });
const B = (name, label) => ({ name, label: label || name, type: "Bool" });
const F = (name, label) => ({ name, label: label || name, type: "Float" });
const PK = (f) => ({ ...f, primary_key: true, required: true });

const TABLES = [
  {
    name: "bd_page",
    description: "Birddog pages (views._page_base: core.page + parent title, counts)",
    schema: "views",
    table_name: "_page_base",
    read_only: true,
    min_role_write: 1,
    fields: [
      PK(I("id", "ID")), S("url", "URL"), S("title", "Title"), S("domain", "Domain"),
      I("parent_id", "Parent ID"), S("label", "Label"), S("root_label", "Root label"),
      S("seq_label", "Seq label"), S("level", "Level"),
      S("native_description", "Native description"), S("description_en", "Description (en)"),
      S("years", "Years"), D("wiki_timestamp", "Wiki timestamp"),
      S("availability", "Availability"), S("source_type", "Source type"), B("stale", "Stale"),
      D("last_birddog_update", "Last Birddog update"), S("comments", "Comments"),
      D("reference_date", "Reference date"), D("change_date", "Change date"),
      S("import_message", "Import message"), D("last_imported", "Last imported"),
      S("parent", "Parent"), I("children_count", "Children"),
      I("doc_links_count", "Documents"),
    ],
  },
  {
    name: "bd_document",
    description: "Birddog documents (core.document); curators edit the curation fields",
    schema: "core",
    table_name: "document",
    read_only: false,
    min_role_write: STAFF,
    fields: [
      PK(I("id", "ID")), { ...S("url", "URL"), required: true }, S("title", "Title"),
      S("domain", "Domain"), S("wikisource_url", "Wikisource URL"), S("filename", "Filename"),
      D("wiki_timestamp", "Wiki timestamp"), I("byte_size", "Bytes"), S("mimetype", "MIME type"),
      S("mediatype", "Media type"), I("width", "Width"), I("height", "Height"),
      I("page_count", "Page count"), S("sha1_hash", "SHA-1"), S("description_url", "Description URL"),
      S("availability", "Availability"), S("lookup_status", "Lookup status"),
      S("location_status", "Location status"),
      { ...B("dupes_checked", "Dupes checked"), required: true },
      { ...B("anomaly", "Anomaly"), required: true }, S("anomaly_comment", "Anomaly comment"),
      D("last_birddog_update", "Last Birddog update"), S("description", "Description"),
      S("process_code", "Process code"), S("save_status", "Save status"),
      S("doc_structure", "Doc structure"), S("doc_scope", "Doc scope"), S("language", "Language"),
      S("processor", "Processor"), S("pages_processed", "Pages processed"),
      S("when_transcribed", "When transcribed"), S("comments", "Comments"),
      D("reference_date", "Reference date"), S("import_message", "Import message"),
      D("last_imported", "Last imported"),
    ],
  },
  {
    name: "bd_page_document",
    description: "Page-document links with titles (saltcorn.page_document_link)",
    schema: "saltcorn",
    table_name: "page_document_link",
    read_only: true,
    min_role_write: 1,
    fields: [
      PK(S("link_id", "Link")), I("page_id", "Page ID"), S("page_title", "Page"),
      S("page_url", "Page URL"), I("document_id", "Document ID"), S("document_title", "Document"),
      S("document_url", "Document URL"), S("process_code", "Process code"),
      S("save_status", "Save status"), S("doc_scope", "Doc scope"),
    ],
  },
  {
    name: "bd_search_request",
    description: "Search texts (saltcorn.search_request); inserting one runs core.search",
    schema: "saltcorn",
    table_name: "search_request",
    read_only: false,
    min_role_write: STAFF,
    fields: [PK(I("id", "ID")), { ...S("q", "Search"), required: true }, D("requested_at", "Requested at")],
  },
  {
    name: "bd_search_hit",
    description: "core.search results per search text (saltcorn.search_hit)",
    schema: "saltcorn",
    table_name: "search_hit",
    read_only: true,
    min_role_write: 1,
    fields: [
      PK(S("hit_id", "Hit")), S("q", "Search"), S("kind", "Kind"), I("id", "ID"), S("title", "Title"),
      S("url", "URL"), S("snippet", "Snippet"), F("score", "Score"),
    ],
  },
];

// -------------------------------------------------------------- layout helpers
const text = (contents, textStyle = "") => ({ type: "blank", contents, textStyle, block: false });
const field = (field_name, fieldview = "as_text") => ({ type: "field", field_name, fieldview, textStyle: "", block: false });
// link whose text and url are JS expressions over the row
const link = (textExpr, urlExpr, blank = false) => ({
  type: "link", text: textExpr, url: urlExpr, isFormula: { text: true, url: true },
  link_target_blank: blank, block: false, textStyle: "",
});
const embed = (viewname, extra_state_fml) => ({
  type: "view", view: `Independent:${viewname}`, name: viewname, state: "shared",
  ...(extra_state_fml ? { extra_state_fml } : {}),
});
const row = (label, contents) => ({
  widths: [2, 10], aligns: ["end", "start"], breakpoints: ["md", "md"], mobileAligns: ["start"],
  style: { "margin-bottom": "0.75rem" },
  besides: [{ above: [null, text(label)] }, { above: [null, contents] }],
});
const editRow = (label, name) => ({
  ...row(label, field(name, "edit")),
  besides: [{ above: [null, { ...text(label), labelFor: name }] }, { above: [null, field(name, "edit")] }],
});

// List layout: [{label, contents}] -> {columns, layout}
const listCfg = (cols) => ({
  columns: cols.map(({ contents: c, label }) =>
    c.type === "field"
      ? { type: "Field", field_name: c.field_name, fieldview: c.fieldview, header_label: label }
      : { type: "Link", link_text: c.text, link_text_formula: true, link_url: c.url, link_url_formula: true, link_target_blank: c.link_target_blank, header_label: label }),
  layout: { list_columns: true, besides: cols.map(({ contents, label }) => ({ contents, header_label: label })) },
});

// columns for Show/Edit: every field and link in a layout
const columnsOf = (layout) => {
  const out = [];
  const walk = (s) => {
    if (!s || typeof s !== "object") return;
    if (Array.isArray(s)) return s.forEach(walk);
    if (s.type === "field") out.push({ type: "Field", field_name: s.field_name, fieldview: s.fieldview });
    else if (s.type === "action") out.push({ type: "Action", action_name: s.action_name });
    else if (s.type === "view") out.push({ type: "View", view: s.view, name: s.name, state: s.state });
    else if (s.type === "link") out.push({ type: "Link", link_text: s.text, link_url: s.url, link_text_formula: true, link_url_formula: true });
    Object.values(s).forEach(walk);
  };
  walk(layout);
  return out;
};

const pageUrl = "'/view/curator_page?id=' + id";
const docUrl = "'/view/curator_document?id=' + id";

// -------------------------------------------------------------- views
const VIEWS = () => {
  const searchFormLayout = {
    above: [
      editRow("Search", "q"),
      {
        widths: [2, 10], aligns: ["end", "start"], style: { "margin-bottom": "1.5rem" },
        besides: [null, { type: "action", action_name: "Save", action_label: "Search", action_style: "btn-primary", minRole: STAFF, block: false }],
      },
    ],
  };
  const pageLayout = {
    above: [
      { ...field("title"), textStyle: "h3" },
      row("Wiki page", link("url", "url", true)),
      row("Parent", link("parent || ''", "parent_id ? '/view/curator_page?id=' + parent_id : ''")),
      row("Level", field("level")),
      row("Label", field("label")),
      row("Archive", field("root_label")),
      row("Years", field("years")),
      row("Domain", field("domain")),
      row("Availability", field("availability")),
      row("Source type", field("source_type")),
      row("Stale", field("stale", "show")),
      row("Description (en)", field("description_en")),
      row("Native description", field("native_description")),
      row("Comments", field("comments")),
      row("Wiki timestamp", field("wiki_timestamp", "show")),
      row("Last Birddog update", field("last_birddog_update", "show")),
      row("Child pages", field("children_count", "show")),
      text("Linked documents", "h5"),
      embed("curator_page_documents", "{page_id: id}"),
    ],
  };
  const docLayout = {
    above: [
      { ...field("title"), textStyle: "h3" },
      row("", link("'Edit curator fields'", "'/view/curator_document_edit?id=' + id")),
      row("Wiki file", link("url", "url", true)),
      row("Wikisource", link("wikisource_url || ''", "wikisource_url || ''", true)),
      row("Filename", field("filename")),
      row("MIME type", field("mimetype")),
      row("Pages", field("page_count", "show")),
      row("Bytes", field("byte_size", "show")),
      row("Availability", field("availability")),
      row("Lookup status", field("lookup_status")),
      row("Location status", field("location_status")),
      row("SHA-1", field("sha1_hash")),
      text("Curation", "h5"),
      row("Description", field("description")),
      row("Process code", field("process_code")),
      row("Save status", field("save_status")),
      row("Doc structure", field("doc_structure")),
      row("Doc scope", field("doc_scope")),
      row("Language", field("language")),
      row("Processor", field("processor")),
      row("Pages processed", field("pages_processed")),
      row("When transcribed", field("when_transcribed")),
      row("Comments", field("comments")),
      row("Reference date", field("reference_date", "show")),
      row("Anomaly", field("anomaly", "show")),
      row("Anomaly comment", field("anomaly_comment")),
      text("Owning pages", "h5"),
      embed("curator_document_pages", "{document_id: id}"),
    ],
  };
  const editFields = [
    ["Description", "description"], ["Process code", "process_code"], ["Save status", "save_status"],
    ["Doc structure", "doc_structure"], ["Doc scope", "doc_scope"], ["Language", "language"],
    ["Processor", "processor"], ["Pages processed", "pages_processed"],
    ["When transcribed", "when_transcribed"], ["Comments", "comments"],
    ["Reference date", "reference_date"], ["Anomaly", "anomaly"], ["Anomaly comment", "anomaly_comment"],
  ];
  const docEditLayout = {
    above: [
      text("Edit curator fields", "h3"),
      row("", { ...link("Back to document", docUrl), isFormula: { text: false, url: true } }),
      ...editFields.map(([l, n]) => editRow(l, n)),
      {
        widths: [2, 10], aligns: ["end", "start"], style: { "margin-bottom": "1.5rem" },
        besides: [null, { type: "action", action_name: "Save", action_style: "btn-primary", minRole: STAFF, block: false }],
      },
    ],
  };
  return [
    {
      name: "curator_search_form", table: "bd_search_request", viewtemplate: "Edit",
      configuration: {
        layout: searchFormLayout, columns: columnsOf(searchFormLayout), fixed: {},
        destination_type: "URL formula",
        dest_url_formula: "'/page/curator_search?q=' + encodeURIComponent(q)",
      },
    },
    {
      name: "curator_search_results", table: "bd_search_hit", viewtemplate: "List",
      configuration: {
        ...listCfg([
          { label: "Kind", contents: field("kind") },
          { label: "Title", contents: link("title || url", `kind === 'page' ? ${pageUrl} : ${docUrl}`) },
          { label: "Snippet", contents: field("snippet") },
          { label: "Wiki", contents: link("'open'", "url", true) },
        ]),
        default_state: {
          // without a search text show nothing (instead of every logged search)
          include_fml: "q === $q",
          _order_field: "score", _descending: true, _rows_per_page: 50, _omit_state_form: true,
        },
      },
    },
    {
      name: "curator_page", table: "bd_page", viewtemplate: "Show",
      configuration: { layout: pageLayout, columns: columnsOf(pageLayout) },
    },
    {
      name: "curator_page_documents", table: "bd_page_document", viewtemplate: "List",
      configuration: {
        ...listCfg([
          { label: "Document", contents: link("document_title || document_url", "'/view/curator_document?id=' + document_id") },
          { label: "Process code", contents: field("process_code") },
          { label: "Save status", contents: field("save_status") },
          { label: "Doc scope", contents: field("doc_scope") },
          { label: "Wiki", contents: link("'open'", "document_url", true) },
        ]),
        default_state: {
          include_fml: "page_id === $page_id",
          _order_field: "document_title", _rows_per_page: 100, _omit_state_form: true,
        },
      },
    },
    {
      name: "curator_document", table: "bd_document", viewtemplate: "Show",
      configuration: { layout: docLayout, columns: columnsOf(docLayout) },
    },
    {
      name: "curator_document_pages", table: "bd_page_document", viewtemplate: "List",
      configuration: {
        ...listCfg([
          { label: "Page", contents: link("page_title || page_url", "'/view/curator_page?id=' + page_id") },
          { label: "Wiki", contents: link("'open'", "page_url", true) },
        ]),
        default_state: {
          include_fml: "document_id === $document_id",
          _order_field: "page_title", _rows_per_page: 100, _omit_state_form: true,
        },
      },
    },
    {
      name: "curator_document_edit", table: "bd_document", viewtemplate: "Edit",
      configuration: {
        layout: docEditLayout, columns: columnsOf(docEditLayout), fixed: {},
        destination_type: "URL formula",
        dest_url_formula: docUrl,
      },
    },
  ];
};

const PAGES = [
  {
    name: "curator_search",
    title: "Curator search",
    description: "Search Birddog pages and documents (core.search)",
    layout: {
      above: [
        text("Search pages and documents", "h3"),
        { type: "view", view: "curator_search_form", name: "curator_search_form", state: "shared" },
        { type: "view", view: "curator_search_results", name: "curator_search_results", state: "shared" },
      ],
    },
  },
];

const MENU = {
  type: "Header", label: "Curator", text: "Curator", icon: "fas fa-feather", location: "Standard",
  min_role: STAFF, max_role: "1", style: "", tooltip: "", target: "_self", href: "",
  subitems: [{
    type: "Page", label: "Search", text: "Search", pagename: "curator_search", icon: "fas fa-search",
    location: "Standard", min_role: STAFF, max_role: "1", style: "", tooltip: "", target: "_self",
    href: "", in_modal: false, target_blank: false, disable_on_mobile: false,
  }],
};

// -------------------------------------------------------------- apply
(async () => {
  const { init_some_tenants } = require("/usr/local/lib/node_modules/@saltcorn/cli/src/common");
  await init_some_tenants();
  const Table = require("@saltcorn/data/models/table");
  const View = require("@saltcorn/data/models/view");
  const Page = require("@saltcorn/data/models/page");
  const { getState } = require("@saltcorn/data/db/state");

  for (const t of TABLES) {
    const provider_cfg = { ...CONN, schema: t.schema, table_name: t.table_name, read_only: t.read_only, fields: t.fields };
    const opts = { provider_name: "PostgreSQL remote table", provider_cfg, min_role_read: STAFF, min_role_write: t.min_role_write, description: t.description };
    const existing = Table.findOne({ name: t.name });
    if (existing) await existing.update(opts);
    else await Table.create(t.name, opts);
    console.log("table", t.name, existing ? "updated" : "created");
  }
  await getState().refresh_tables();

  for (const v of VIEWS()) {
    const table = Table.findOne({ name: v.table });
    const rec = { name: v.name, table_id: table.id, viewtemplate: v.viewtemplate, configuration: v.configuration, min_role: STAFF };
    const existing = View.findOne({ name: v.name });
    if (existing) await View.update(rec, existing.id);
    else await View.create(rec);
    console.log("view", v.name, existing ? "updated" : "created");
  }
  await getState().refresh_views();

  for (const p of PAGES) {
    const rec = { ...p, min_role: STAFF, fixed_states: {} };
    const existing = Page.findOne({ name: p.name });
    if (existing) await Page.update(existing.id, rec);
    else await Page.create(rec);
    console.log("page", p.name, existing ? "updated" : "created");
  }
  await getState().refresh_pages();

  const menu = (getState().getConfig("menu_items", []) || []).filter((m) => m.label !== MENU.label);
  menu.unshift(MENU);
  await getState().setConfig("menu_items", menu);
  console.log("menu: Curator > Search");
  process.exit(0);
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
