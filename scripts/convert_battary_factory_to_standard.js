#!/usr/bin/env node
/* Convert the CCPL battery sample into direct ccAPPS input-model records. */
const fs = require("fs");

const input = process.argv[2] || "battary_factory.json";
const output = process.argv[3] || "battary_factory_standard.json";
const request = JSON.parse(fs.readFileSync(input, "utf8"));
const data = request.data || request;
const source = data.source || (data.dataset && data.dataset.id) || "battery_2d_line";
const itemName = (id, row) =>
  (data.itemNames && data.itemNames[id]) || `ccpl-item-${row.materialCode || id}`;
const resourceName = (id, row) =>
  (data.resourceNames && data.resourceNames[id]) || `ccpl-resource-${row.resourceCode || id}`;
const operationName = (row) => `ccpl-operation-${row.operationCode || row.id}`;
const calendarName = (id) => `ccpl-calendar-${resourceName(id, {})}`;
const defaultLocation = data.defaultLocationName || `ccpl-location-${source}`;
const materialById = new Map(data.materials.map((row) => [row.id, row]));
const resourceById = new Map(data.resources.map((row) => [row.id, row]));
const materialLocation = (id) => (materialById.get(id) || {}).locationName || defaultLocation;

const locations = new Map();
function addLocation(name) {
  name = name || defaultLocation;
  if (!locations.has(name)) locations.set(name, { name, description: name, source });
  return name;
}
addLocation(defaultLocation);

const calendars = [];
const calendarBuckets = [];
const calendarRows = data.calendarsByResourceId || {};
for (const [resourceId, rows] of Object.entries(calendarRows)) {
  const name = calendarName(resourceId);
  calendars.push({ name, description: `Imported resource calendar ${resourceId}`, defaultvalue: 0, source });
  rows.forEach((row, index) => {
    if (!row.calendarDate) return;
    calendarBuckets.push({
      id: `${name}-${row.calendarDate}`,
      calendar: name,
      startdate: row.calendarDate,
      enddate: row.calendarDate,
      starttime: row.shiftStartTime || "00:00:00",
      endtime: row.shiftEndTime || "23:59:59",
      value: row.workCapacity == null ? 1 : row.workCapacity,
      priority: index,
      monday: true, tuesday: true, wednesday: true, thursday: true,
      friday: true, saturday: true, sunday: true,
      source,
    });
  });
}

const items = data.materials.map((row) => {
  addLocation(row.locationName);
  return { name: itemName(row.id, row), description: row.materialName, category: row.materialType,
    cost: row.unitCost || 0, uom: row.unitName, source };
});
const buffers = data.materials.map((row) => ({
  id: `${itemName(row.id, row)} @ ${addLocation(row.locationName)}`,
  description: `${itemName(row.id, row)} @ ${materialLocation(row.id)}`,
  item: itemName(row.id, row), location: materialLocation(row.id), batch: "",
  onhand: row.currentStock || 0, source,
}));

const skillNames = new Set(data.resources.map((row) => row.skillName).filter(Boolean));
for (const row of data.operations) if (row.requiredSkillName) skillNames.add(row.requiredSkillName);
const skills = [...skillNames].sort().map((name) => ({ name, source }));
const resources = data.resources.map((row) => {
  addLocation(row.locationName);
  return { name: resourceName(row.id, row), description: row.resourceName, type: row.resourceType || "default",
    maximum: row.capacity == null ? 1 : row.capacity, efficiency: (row.efficiency == null ? 1 : row.efficiency) * 100,
    constrained: Boolean(row.isEnabled == null ? 1 : row.isEnabled), location: row.locationName || defaultLocation,
    available: calendarRows[row.id] ? calendarName(row.id) : null, source };
});
const resourceSkills = data.resources.filter((row) => row.skillName).map((row) => ({
  id: `${resourceName(row.id, row)} - ${row.skillName}`,
  resource: resourceName(row.id, row), skill: row.skillName, priority: 1,
  effective_start: "1971-01-01T00:00:00", effective_end: "2030-12-31T00:00:00", source,
}));

const producedByOperation = new Map();
for (const row of data.operationMaterials) if (row.materialRole === "produce") producedByOperation.set(row.operationId, row.materialId);
const operations = data.operations.map((row) => {
  const resource = resourceById.get(row.resourceId);
  const produced = producedByOperation.get(row.id);
  const location = produced ? materialLocation(produced) : (resource && resource.locationName) || defaultLocation;
  addLocation(location);
  return { name: operationName(row), type: "time_per", description: row.operationName,
    item: produced ? itemName(produced, materialById.get(produced)) : null, location,
    priority: row.sequenceNo || row.priority || 1, duration: row.fixedDurationSeconds || null,
    duration_per: row.unitDurationSeconds || null, sizeminimum: row.sizeMinimum == null ? 1 : row.sizeMinimum,
    sizemultiple: row.sizeMultiple == null ? null : row.sizeMultiple, sizemaximum: row.sizeMaximum == null ? null : row.sizeMaximum,
    source };
});
const operationById = new Map(data.operations.map((row) => [row.id, row]));
const operationResources = data.operations.filter((row) => resourceById.has(row.resourceId)).map((row) => ({
  id: `${operationName(row)} - ${resourceName(row.resourceId, resourceById.get(row.resourceId))}`,
  operation: operationName(row), resource: resourceName(row.resourceId, resourceById.get(row.resourceId)),
  skill: row.requiredSkillName || null, quantity: row.resourceQuantity == null ? 1 : row.resourceQuantity,
  priority: 1, effective_start: "1971-01-01T00:00:00", effective_end: "2030-12-31T00:00:00", source,
}));
const operationMaterials = data.operationMaterials.map((row) => {
  const operation = operationById.get(row.operationId);
  const item = materialById.get(row.materialId);
  return { id: row.id, operation: operationName(operation), item: itemName(row.materialId, item),
    location: operation ? operations.find((x) => x.name === operationName(operation)).location : materialLocation(row.materialId),
    quantity: row.materialRole === "consume" ? -Number(row.quantity || 1) : Number(row.quantity || 1),
    type: ["transfer_batch", "transferBatch"].includes(row.issueTiming) ? "transfer_batch" : (row.issueTiming || "start"),
    transferbatch: row.transferBatch == null ? null : row.transferBatch, offset: row.offsetSeconds == null ? null : row.offsetSeconds,
    priority: row.priority || 1, effective_start: "1971-01-01T00:00:00", effective_end: "2030-12-31T00:00:00", source };
});
// Material stocks can reside in a warehouse while an operation runs elsewhere.
// Model that explicitly so the planner can replenish the operation's location.
const distributions = new Map();
for (const row of operationMaterials) {
  const op = operations.find((candidate) => candidate.name === row.operation);
  const stockLocation = materialLocation(
    data.operationMaterials.find((sourceRow) => sourceRow.id === row.id).materialId,
  );
  if (!op || stockLocation === op.location) continue;
  const origin = row.quantity < 0 ? stockLocation : op.location;
  const location = row.quantity < 0 ? op.location : stockLocation;
  const key = `${row.item}|${origin}|${location}`;
  distributions.set(key, { item: row.item, origin, location, leadtime: 0, cost: 0, priority: 1, source });
}

const customers = [...new Set(data.demands.map((row) => row.customerName || "ccpl-customer"))].map((name) => ({ name, description: name, source }));
const demands = data.demands.map((row) => {
  const location = row.deliveryLocationName || materialLocation(row.materialId); addLocation(location);
  return { name: row.demandCode || row.id, description: row.remark || null, customer: row.customerName || "ccpl-customer",
    item: itemName(row.materialId, materialById.get(row.materialId)), location, due: row.dueTime,
    status: (row.status || "open").toLowerCase(), quantity: row.quantity || 0, priority: row.priority || 10, source };
});

const outputData = { version: "1.0", scenario: request.scenario, source, mode: "replace_source", clean_all: true,
  parameters: data.request && data.request.planStart ? [{ name: "currentdate", value: data.request.planStart }] : [],
  plan_options: { constraint: request.constraint || "capa,mfg_lt,po_lt", plantype: request.plantype || 1, env: request.env || "supply" },
  data: { locations: [...locations.values()], calendars, calendarBuckets, items, buffers, skills, resources, resourceSkills, operations, operationResources, operationMaterials, itemDistributions: [...distributions.values()], customers, demands } };
fs.writeFileSync(output, `${JSON.stringify(outputData, null, 2)}\n`, "utf8");
console.log(`Wrote ${output}`);
