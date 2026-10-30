// Embedded in n8n Code nodes; no imports, network access or external packages.
class RowError extends Error {}

function objectValue(value, label) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${label}: objet JSON attendu.`);
  }
  return value;
}

function objectList(value, label) {
  if (!Array.isArray(value)) throw new Error(`${label}: liste JSON attendue.`);
  return value.map(item => objectValue(item, label));
}

function sortedUnique(values) {
  return [...new Set(values)].sort();
}

function same(left, right) {
  if (left === right) return true;
  if (left === null || right === null || typeof left !== typeof right) return false;
  if (typeof left !== 'object' || Array.isArray(left) !== Array.isArray(right)) return false;
  const keys = Object.keys(left).sort();
  const otherKeys = Object.keys(right).sort();
  return keys.length === otherKeys.length &&
    keys.every((key, index) => key === otherKeys[index] && same(left[key], right[key]));
}

function recordId(value) {
  return typeof value === 'string' && /^rec[A-Za-z0-9]+$/.test(value);
}

function dateParts(value, withTime) {
  const pattern = withTime
    ? /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})?$/
    : /^(\d{4})-(\d{2})-(\d{2})$/;
  const match = typeof value === 'string' && value.match(pattern);
  if (!match) throw new RowError('Date ISO invalide.');
  const [year, month, day, hour = 0, minute = 0, second = 0] =
    match.slice(1, 7).map(part => Number(part || 0));
  const base = new Date(0);
  base.setUTCFullYear(year, month - 1, day);
  base.setUTCHours(hour, minute, second, 0);
  if (year < 1 || base.getUTCFullYear() !== year || base.getUTCMonth() !== month - 1 ||
      base.getUTCDate() !== day || base.getUTCHours() !== hour ||
      base.getUTCMinutes() !== minute || base.getUTCSeconds() !== second) {
    throw new RowError('Date ou heure invalide.');
  }
  return {year, month, day, hour, minute, second, seconds: base.getTime() / 1000,
    fraction: Number(match[7] || 0), offset: match[8]};
}

function timestamp(value, naiveTimezone) {
  const parts = dateParts(value, true);
  if (parts.offset) {
    let offset = 0;
    if (parts.offset !== 'Z') {
      const hours = Number(parts.offset.slice(1, 3));
      const minutes = Number(parts.offset.slice(4, 6));
      if (hours > 23 || minutes > 59) throw new RowError('Fuseau invalide.');
      offset = (hours * 3600 + minutes * 60) * (parts.offset[0] === '+' ? 1 : -1);
    }
    return parts.seconds - offset + parts.fraction;
  }
  let formatter;
  try {
    formatter = new Intl.DateTimeFormat('en-GB', {
      timeZone: naiveTimezone, year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
    });
  } catch {
    throw new RowError('Fuseau invalide.');
  }
  const localParts = seconds => Object.fromEntries(
    formatter.formatToParts(new Date(seconds * 1000))
      .filter(part => part.type !== 'literal').map(part => [part.type, Number(part.value)]),
  );
  const offsets = new Set();
  // Sample both sides of any nearby DST transition, then validate each candidate.
  for (const delta of [-172800, -86400, 0, 86400, 172800]) {
    const instant = parts.seconds + delta;
    const local = localParts(instant);
    const localDate = new Date(0);
    localDate.setUTCFullYear(local.year, local.month - 1, local.day);
    localDate.setUTCHours(local.hour, local.minute, local.second, 0);
    offsets.add(localDate.getTime() / 1000 - instant);
  }
  const matches = [];
  for (const offset of offsets) {
    const candidate = parts.seconds - offset;
    const local = localParts(candidate);
    if (['year', 'month', 'day', 'hour', 'minute', 'second'].every(key => local[key] === parts[key])) {
      matches.push(candidate);
    }
  }
  if (matches.length !== 1) {
    throw new RowError("Heure locale ambiguë ou inexistante au changement d'heure.");
  }
  return matches[0] + parts.fraction;
}

function convert(field, value, references) {
  const kind = field.conversion;
  if (!['text', 'choice', 'number', 'integer_checked', 'boolean', 'date', 'datetime',
    'choice_list', 'reference_list'].includes(kind)) {
    throw new RowError('Conversion non prise en charge.');
  }
  if (value === null || value === '' || (Array.isArray(value) && value.length === 0)) {
    if (kind === 'text' || kind === 'choice') return '';
    return kind === 'boolean' ? false : null;
  }
  if (kind === 'text') {
    if (typeof value !== 'string') throw new RowError('Texte attendu.');
    return value;
  }
  if (kind === 'number' || kind === 'integer_checked') {
    if (typeof value !== 'number' || !Number.isFinite(value)) throw new RowError('Nombre fini attendu.');
    if (Number.isInteger(value) && !Number.isSafeInteger(value)) {
      throw new RowError('Entier hors de la précision sûre JavaScript.');
    }
    if (kind === 'integer_checked' && !Number.isInteger(value)) throw new RowError('Entier attendu.');
    return value;
  }
  if (kind === 'boolean') {
    if (typeof value !== 'boolean') throw new RowError('Booléen attendu.');
    return value;
  }
  if (kind === 'choice') {
    if (typeof value !== 'string' || !field.choices.includes(value)) {
      throw new RowError('Choix non autorisé dans le mapping validé.');
    }
    return value;
  }
  if (kind === 'choice_list' || kind === 'reference_list') {
    if (!Array.isArray(value) || !value.every(item => typeof item === 'string') ||
        new Set(value).size !== value.length) throw new RowError('Liste invalide ou en doublon.');
    if (kind === 'choice_list') {
      if (value.some(item => !field.choices.includes(item))) throw new RowError('Choix non autorisé.');
      return ['L', ...value];
    }
    if (!references) throw new RowError('Index des références absent.');
    return ['L', ...value.map(item => {
      const ids = references.get(item) || [];
      if (ids.length !== 1) throw new RowError('Référence liée absente ou en doublon dans Grist.');
      return ids[0];
    })];
  }
  if (kind === 'date') return dateParts(value, false).seconds;
  return timestamp(value, field.naive_timezone);
}

function equalCell(before, after, gristType) {
  if (Array.isArray(before) && before.length && before[0] !== 'L') {
    throw new RowError('Valeur Grist masquée, en erreur ou non calculée.');
  }
  if (['ChoiceList', 'RefList'].includes(gristType.split(':')[0]) && same(before, ['L'])) {
    before = null;
  }
  return same(before, after);
}

function indexGrist(records, keyColumn, tableId) {
  const index = new Map();
  const byId = new Map();
  const anomalies = [];
  for (const record of records) {
    const rowId = record.id;
    if (!Number.isSafeInteger(rowId) || rowId <= 0 || byId.has(rowId)) {
      throw new Error('Identifiant de ligne Grist absent, invalide ou répété.');
    }
    const fields = objectValue(record.fields, 'Grist record fields');
    byId.set(rowId, record);
    const key = fields[keyColumn];
    if (!recordId(key)) {
      anomalies.push({table_id: tableId, grist_record_id: rowId,
        code: key == null || key === '' ? 'missing_key' : 'invalid_key'});
      continue;
    }
    if (!index.has(key)) index.set(key, []);
    index.get(key).push(rowId);
  }
  for (const [key, ids] of index) {
    if (ids.length > 1) anomalies.push({
      table_id: tableId, airtable_record_id: key, grist_record_ids: ids, code: 'duplicate_key',
    });
  }
  return {index, byId, anomalies};
}

function schemaContract(mapping, schema) {
  const sources = schema.airtable.tables.filter(table => table.id === mapping.source.table_id);
  if (sources.length !== 1) throw new Error('Table Airtable absente ou ambiguë.');
  const wanted = new Set([mapping.modified_field_id, ...mapping.fields.map(f => f.airtable_field_id)]);
  const sourceFields = Object.fromEntries(sources[0].fields
    .filter(field => wanted.has(field.id)).map(field => [field.id, field]));
  if (Object.keys(sourceFields).length !== wanted.size) throw new Error('Champ Airtable absent.');
  const wantedColumns = new Map([[mapping.target.table_id, new Set([
    mapping.record_key.target_column, ...mapping.fields.map(f => f.grist_column_id),
  ])]]);
  for (const field of mapping.fields) {
    if (field.conversion !== 'reference_list') continue;
    const lookup = field.lookup;
    if (!wantedColumns.has(lookup.grist_table_id)) wantedColumns.set(lookup.grist_table_id, new Set());
    wantedColumns.get(lookup.grist_table_id).add(lookup.grist_column_id);
  }
  const targets = {};
  for (const [tableId, ids] of wantedColumns) {
    const tables = schema.grist.tables.filter(table => table.id === tableId);
    if (tables.length !== 1) throw new Error('Table Grist absente ou ambiguë.');
    const columns = Object.fromEntries(tables[0].columns
      .filter(column => ids.has(column.id)).map(column => [column.id, column]));
    if (Object.keys(columns).length !== ids.size) throw new Error('Colonne Grist absente.');
    targets[tableId] = columns;
  }
  return {airtable_fields: sourceFields, grist_columns: targets};
}

function prepare(context) {
  const config = objectValue(context.config, 'config');
  if (!['simulation', 'write'].includes(config.mode)) throw new Error('Mode inconnu.');
  if (config.mode === 'write' && (config.write_confirmation !== 'WRITE_AIRTABLE_TO_GRIST' ||
      config.serialized_executions_confirmed !== true ||
      config.modified_field_coverage_confirmed !== true)) {
    throw new Error('Écriture refusée : confirmation, sérialisation et couverture de updatedat requises.');
  }
  if (typeof config.force_full_scan !== 'boolean') throw new Error('force_full_scan doit être booléen.');
  if (!Number.isSafeInteger(config.overlap_seconds) || config.overlap_seconds < 120) {
    throw new Error('Fenêtre de recouvrement invalide.');
  }
  const rows = objectList(context.state_rows, 'state_rows').filter(row => Object.keys(row).length);
  if (rows.length !== 1 || rows[0].sync_key !== config.state_key) {
    throw new Error("Créer exactement une ligne d'état pour cette synchronisation.");
  }
  const row = rows[0];
  if (!(typeof row.id === 'string' && row.id.length > 0) &&
      !(Number.isSafeInteger(row.id) && row.id !== 0)) throw new Error("Identifiant d'état invalide.");
  if (typeof row.payload !== 'string') throw new Error("L'état doit être un document JSON.");
  const state = objectValue(JSON.parse(row.payload), 'state.payload');
  if (state.version !== 1 || !Object.hasOwn(state, 'cursor') ||
      !Array.isArray(state.retry_ids) || !state.retry_ids.every(recordId) ||
      new Set(state.retry_ids).size !== state.retry_ids.length) throw new Error('État invalide.');
  const bootstrap = state.cursor === null && state.retry_ids.length === 0 &&
    state.mapping_fingerprint === '';
  if (!bootstrap && state.mapping_fingerprint !== config.mapping_fingerprint) {
    throw new Error("Le mapping a changé : réinitialisation contrôlée de l'état requise.");
  }
  const startedAt = context.started_at || new Date().toISOString();
  const startSeconds = timestamp(startedAt, 'UTC');
  let formula = null;
  if (state.cursor !== null) {
    if (typeof state.cursor !== 'string' || !/(Z|\+00:00)$/.test(state.cursor)) {
      throw new Error('Le curseur doit être un horodatage UTC explicite.');
    }
    const cursorSeconds = timestamp(state.cursor, 'UTC');
    if (cursorSeconds > startSeconds) throw new Error('Le curseur est dans le futur.');
    const cutoff = new Date((cursorSeconds - config.overlap_seconds) * 1000).toISOString();
    const terms = [
      `IS_AFTER({${config.mapping.modified_field_id}}, DATETIME_PARSE('${cutoff}'))`,
      `IS_AFTER(CREATED_TIME(), DATETIME_PARSE('${cutoff}'))`,
      ...state.retry_ids.map(id => `RECORD_ID()='${id}'`),
    ];
    if (!config.force_full_scan) formula = `OR(${terms.join(',')})`;
  }
  const result = {...context, state_row_id: row.id, state, started_at: startedAt,
    filter_formula: formula, read_mode: formula ? 'incremental' : 'full'};
  delete result.state_rows;
  return result;
}

function validate_schema(context) {
  const source = objectValue(context.source_schema, 'source_schema');
  const live = {
    airtable: {tables: objectList(source.tables, 'Airtable tables')},
    grist: {tables: Object.entries(objectValue(context.target_schemas, 'target_schemas'))
      .map(([id, payload]) => ({id, columns: objectList(payload.columns, 'Grist columns')}))},
  };
  if (!same(schemaContract(context.config.mapping, live), context.config.schema_contract)) {
    throw new Error('Le schéma a changé depuis la validation ; nouvelle revue requise.');
  }
  const result = {...context};
  delete result.source_schema;
  delete result.target_schemas;
  return result;
}

function prepare_plan(context) {
  const pages = objectList(context.airtable_pages, 'airtable_pages');
  if (!pages.length || pages[pages.length - 1].offset != null) {
    throw new Error('Pagination Airtable incomplète ; aucune écriture autorisée.');
  }
  const seenOffsets = new Set();
  const source = [];
  for (const page of pages) {
    if (page.offset != null) {
      if (typeof page.offset !== 'string' || !page.offset || seenOffsets.has(page.offset)) {
        throw new Error('Pagination Airtable invalide ou répétée.');
      }
      seenOffsets.add(page.offset);
    }
    source.push(...objectList(page.records, 'Airtable records'));
  }
  const mapping = context.config.mapping;
  const target = objectList(context.target_records.records, 'Grist records');
  const targetIndex = indexGrist(target, mapping.record_key.target_column, mapping.target.table_id);
  const anomalies = [...targetIndex.anomalies];
  const referenceIndexes = new Map();
  for (const field of mapping.fields) {
    if (field.conversion !== 'reference_list') continue;
    const lookup = field.lookup;
    const key = JSON.stringify([lookup.grist_table_id, lookup.grist_column_id]);
    if (!referenceIndexes.has(key)) {
      const linked = indexGrist(
        objectList(context.linked_records[lookup.grist_table_id].records, 'Grist linked records'),
        lookup.grist_column_id, lookup.grist_table_id,
      );
      referenceIndexes.set(key, linked.index);
      anomalies.push(...linked.anomalies);
    }
  }
  const sourceCounts = new Map();
  for (const record of source) {
    if (!recordId(record.id)) throw new Error('Identifiant de record Airtable invalide.');
    objectValue(record.fields, 'Airtable record fields');
    sourceCounts.set(record.id, (sourceCounts.get(record.id) || 0) + 1);
  }
  const creates = [], updates = [], errors = [];
  let unchanged = 0, missingModified = 0;
  for (const record of source) {
    const key = record.id;
    const fields = record.fields;
    if (!Object.hasOwn(fields, mapping.modified_field_id)) missingModified++;
    const matches = targetIndex.index.get(key) || [];
    if (sourceCounts.get(key) !== 1 || matches.length > 1) {
      errors.push({airtable_record_id: key, code: 'ambiguous_record_key'});
      continue;
    }
    const current = matches.length ? targetIndex.byId.get(matches[0]) : null;
    const values = {}, before = {}, rowErrors = [];
    for (const field of mapping.fields) {
      const sourceId = field.airtable_field_id;
      if (!Object.hasOwn(fields, sourceId)) continue;
      const columnId = field.grist_column_id;
      const references = field.conversion === 'reference_list'
        ? referenceIndexes.get(JSON.stringify([
          field.lookup.grist_table_id, field.lookup.grist_column_id,
        ])) : null;
      try {
        const value = convert(field, fields[sourceId], references);
        if (current) {
          const currentErrors = objectValue(current.errors || {}, 'Grist record errors');
          if (Object.hasOwn(currentErrors, columnId) || !Object.hasOwn(current.fields, columnId)) {
            throw new RowError('Colonne Grist inaccessible, absente ou en erreur.');
          }
          const old = current.fields[columnId];
          if (equalCell(old, value, field.grist_type)) continue;
          before[columnId] = old;
        }
        values[columnId] = value;
      } catch (error) {
        if (!(error instanceof RowError)) throw error;
        rowErrors.push({airtable_field_id: sourceId, message: error.message});
      }
    }
    if (rowErrors.length) errors.push({airtable_record_id: key, code: 'invalid_row', fields: rowErrors});
    else if (!current) creates.push({
      airtable_record_id: key, fields: {[mapping.record_key.target_column]: key, ...values},
    });
    else if (Object.keys(values).length) updates.push({
      airtable_record_id: key, id: current.id, fields: values, before,
    });
    else unchanged++;
  }
  const missingRetries = context.state.retry_ids.filter(id => !sourceCounts.has(id)).sort();
  for (const key of missingRetries) errors.push({
    airtable_record_id: key, code: 'retry_record_not_returned',
  });
  const retryIds = sortedUnique(errors.map(error => error.airtable_record_id));
  const report = {
    mode: 'read_only_full_simulation', remote_writes: 0, cursor_advanced: false,
    summary: {
      airtable_records: source.length, grist_records: target.length, mapped_fields: mapping.fields.length,
      creates: creates.length, updates: updates.length, unchanged,
      skipped: errors.length - missingRetries.length,
      changed_cells: updates.reduce((total, row) => total + Object.keys(row.fields).length, 0),
      grist_key_anomalies: anomalies.length, missing_modified_timestamp: missingModified,
    },
    creates, updates, errors, grist_key_anomalies: anomalies, retry_airtable_ids: retryIds,
    warnings: [
      'Les colonnes calculées exclues et la table AODE ne sont jamais écrites.',
      'Les champs absents ne sont pas effacés ; aucune suppression n’est effectuée.',
      'Lecture séquentielle sans instantané transactionnel entre les deux bases.',
    ],
  };
  const operations = [];
  for (const [action, rows] of [['create', creates], ['update', updates]]) {
    for (const row of rows) {
      const values = {...row.fields};
      delete values[mapping.record_key.target_column];
      operations.push({
        action, airtable_record_id: row.airtable_record_id, fields: values,
        body: {records: [{
          require: {[mapping.record_key.target_column]: row.airtable_record_id}, fields: values,
        }]},
      });
    }
  }
  const checkpoint = {
    version: 1, mapping_fingerprint: context.config.mapping_fingerprint,
    cursor: context.state.cursor,
    retry_ids: sortedUnique([...context.state.retry_ids, ...retryIds,
      ...operations.map(operation => operation.airtable_record_id)]),
    pending_since: context.started_at,
  };
  const result = {...context, report, operations, checkpoint_payload: JSON.stringify(checkpoint)};
  delete result.airtable_pages;
  delete result.target_records;
  delete result.linked_records;
  return result;
}

function finalize(context) {
  if (context.config.mode !== 'write') throw new Error('Finalisation interdite en simulation.');
  const {report, operations} = context;
  const results = objectList(context.write_results, 'write_results');
  if (operations.length !== results.length) throw new Error('Résultats d’écriture incomplets.');
  const mapping = context.config.mapping;
  const target = indexGrist(
    objectList(context.verification_records.records, 'verification_records'),
    mapping.record_key.target_column, mapping.target.table_id,
  );
  const fields = new Map(mapping.fields.map(field => [field.grist_column_id, field]));
  let verified = 0;
  const writeErrors = [];
  operations.forEach((operation, index) => {
    const key = operation.airtable_record_id;
    const response = results[index];
    const status = response.status_code;
    if (response.airtable_record_id !== key || !Number.isInteger(status) ||
        status < 200 || status >= 300) {
      writeErrors.push({airtable_record_id: key, code: 'write_http_error', status_code: status ?? null});
      return;
    }
    const matches = target.index.get(key) || [];
    if (matches.length !== 1) {
      writeErrors.push({airtable_record_id: key, code: 'write_key_not_unique'});
      return;
    }
    const current = target.byId.get(matches[0]);
    try {
      for (const [columnId, expected] of Object.entries(operation.fields)) {
        if (Object.hasOwn(current.errors || {}, columnId) || !Object.hasOwn(current.fields, columnId) ||
            !equalCell(current.fields[columnId], expected, fields.get(columnId).grist_type)) {
          throw new RowError('La valeur enregistrée ne correspond pas au plan.');
        }
      }
      verified++;
    } catch (error) {
      if (!(error instanceof RowError)) throw error;
      writeErrors.push({airtable_record_id: key, code: 'write_verification_failed'});
    }
  });
  const retries = sortedUnique([
    ...report.retry_airtable_ids, ...writeErrors.map(error => error.airtable_record_id),
  ]);
  report.write_errors = writeErrors;
  Object.assign(report.summary, {
    verified_writes: verified, write_failures: writeErrors.length, pending_retries: retries.length,
    grist_key_anomalies: target.anomalies.length + report.grist_key_anomalies
      .filter(issue => issue.table_id !== mapping.target.table_id).length,
  });
  return {...context, final_payload: JSON.stringify({
    version: 1, mapping_fingerprint: context.config.mapping_fingerprint,
    cursor: context.started_at, retry_ids: retries, last_summary: report.summary,
  })};
}

function summary(context) {
  const report = context.report;
  const counts = {...report.summary,
    pending_retries: report.summary.pending_retries ?? report.retry_airtable_ids.length};
  return {
    mode: context.config.mode, read_mode: context.read_mode, summary: counts,
    has_anomalies: Boolean(report.errors.length || report.grist_key_anomalies.length ||
      report.write_errors?.length || counts.grist_key_anomalies),
    cursor_advanced: context.config.mode === 'write', warnings: report.warnings,
  };
}
