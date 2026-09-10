#!/usr/bin/env node
/*
 * Rebuild the CCPL request package from the normalized dataset converted from
 * the battery 2D-line workbook.  The former package contained visual-device
 * placeholders and omitted most WIP handoffs, which made replenishment fail.
 */
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const requestPath = path.join(root, "battary_factory.json");
const datasetPath = path.join(root, "battary_factory_data.json");
const request = JSON.parse(fs.readFileSync(requestPath, "utf8"));
const dataset = JSON.parse(fs.readFileSync(datasetPath, "utf8"));

const demandQuantities = {
  "ORD-202606-001": 1500,
  "ORD-202606-002": 2000,
};
for (const demand of dataset.demands) {
  if (Object.hasOwn(demandQuantities, demand.demandCode)) {
    demand.quantity = demandQuantities[demand.demandCode];
  }
}

// The workbook assigns the skill on each process step.  A couple of device
// records retained their animation-system skills, so align each device with
// the skill actually required by its bound operation.
const resourceById = new Map(dataset.resources.map((resource) => [resource.id, resource]));
const batchSizes = {
  "OP-12": { minimum: 128, multiple: 128, maximum: 128 },
};
for (const operation of dataset.operations) {
  const resource = resourceById.get(operation.resourceId);
  if (resource && operation.requiredSkillName) {
    resource.skillName = operation.requiredSkillName;
  }
  const batch = batchSizes[operation.operationCode];
  if (batch) {
    operation.sizeMinimum = batch.minimum;
    operation.sizeMultiple = batch.multiple;
    if (batch.maximum) operation.sizeMaximum = batch.maximum;
    else delete operation.sizeMaximum;

    // The workbook records the capacity of a batch (50/128) in the material
    // quantity columns. CCPL flows are per unit of the operation plan, so a
    // 1:1 process needs normalized flows; batch size belongs on the operation.
    for (const flow of dataset.operationMaterials) {
      if (flow.operationId === operation.id) flow.quantity = 1;
    }
  }
}

// OP-02 is also continuous in this model.  Its workbook values of 50 describe
// a former batch capacity, not a 50:1 material conversion.
for (const flow of dataset.operationMaterials) {
  if (flow.operationId === "op_op_02") flow.quantity = 1;
}

// Model OP-01 through OP-11 as a continuous line.  Intermediate WIP moves in
// single-piece transfer batches, allowing the next station to start as soon as
// material arrives.  Only the final oven remains a discrete 128-piece batch.
const producerByMaterial = new Map();
for (const flow of dataset.operationMaterials) {
  if (flow.materialRole === "produce") producerByMaterial.set(flow.materialId, flow);
}
for (const flow of dataset.operationMaterials) {
  const producer = producerByMaterial.get(flow.materialId);
  if (!producer || flow.materialRole !== "consume") continue;
  const producerOperation = dataset.operations.find((op) => op.id === producer.operationId);
  const consumerOperation = dataset.operations.find((op) => op.id === flow.operationId);
  if (
    producerOperation && consumerOperation &&
    producerOperation.operationCode !== "OP-12" &&
    consumerOperation.operationCode !== "OP-12"
  ) {
    producer.issueTiming = "transfer_batch";
    producer.transferBatch = 1;
    flow.issueTiming = "transfer_batch";
    flow.transferBatch = 1;
  }
}

const repaired = {
  request_id: request.request_id,
  scenario: "scenario5",
  clean: true,
  clean_all: true,
  constraint: request.constraint || "capa,mfg_lt,po_lt",
  plantype: request.plantype || 1,
  env: request.env || "supply",
  data: dataset,
};

fs.writeFileSync(requestPath, `${JSON.stringify(repaired, null, 2)}\n`, "utf8");
