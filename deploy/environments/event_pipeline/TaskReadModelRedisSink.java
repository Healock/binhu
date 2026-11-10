import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.net.URI;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Base64;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import javax.crypto.Cipher;
import javax.crypto.Mac;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.SecretKeySpec;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.apache.flink.configuration.Configuration;
import org.apache.flink.streaming.api.functions.sink.RichSinkFunction;
import org.apache.flink.types.Row;
import redis.clients.jedis.JedisPooled;

/** Writes encrypted task snapshots and HMAC-only indexes to isolated Redis. */
public final class TaskReadModelRedisSink extends RichSinkFunction<Row> {
    private static final long serialVersionUID = 1L;
    private static final Set<String> CHANGED_FIELDS = Set.of(
        "address", "community", "inspector", "check_result", "task_state",
        "small_community", "person_tags", "task_graph", "online_summary");
    private static final Set<String> SOURCE_TABLES = Set.of(
        "t_fullchain", "t_rental_check", "t_police_stats", "t_suspect_unrevoked",
        "t_suspect_return", "t_delivery_industry", "t_group_rental",
        "t_suzhou_police", "t_traffic_police");
    private static final Set<String> VIEW_DOMAINS = Set.of(
        "address_match", "watch", "qmf", "residence", "registration", "review", "task_graph");
    private static final Map<String, Set<String>> STATE_ENUMS = Map.of(
        "address_match", Set.of("suggested", "confirmed", "ambiguous", "unmatched", "invalid", "conflict", "manual_unmatched", "review_required"),
        "watch", Set.of("active", "none"),
        "qmf", Set.of("not_applicable", "not_scanned", "stale", "pending", "completed_match", "completed_mismatch", "not_found", "non_jurisdiction", "error", "ambiguous", "station_mismatch", "unknown_result", "unavailable"),
        "residence", Set.of("not_applicable", "pending", "querying", "registered", "cancelled", "legacy_completed", "confirmed", "error"),
        "registration", Set.of("not_applicable", "awaiting_match", "matched_once", "review_required", "confirmation_pending", "confirmed", "cancelled", "legacy_completed", "pending_establishment"),
        "review", Set.of("not_applicable", "initial_pending", "initial_extension", "deep_pending", "deep_extension", "final_unverifiable", "resolved", "archived", "source_exception", "new_clue", "no_new_clue"),
        "task_graph", Set.of("not_applicable", "blocked", "ready", "active", "satisfied", "cancelled"));
    private static final String APPLY_VIEW_STATE = """
        local current = tonumber(redis.call('HGET',KEYS[1],'domain_revision') or '-1')
        local revision = tonumber(ARGV[1])
        if redis.call('HGET',KEYS[1],'event_id') == ARGV[2] then return 'duplicate' end
        if revision < current then return 'stale' end
        if revision == current and current >= 0 then return 'revision_conflict' end
        local prefix = ARGV[15]
        local domain = ARGV[6]
        local task_id = ARGV[5]
        local row_group = redis.call('HGET',KEYS[3],'row_group_token') or ''
        local function rebuild_domain_group(token)
          if token == '' then return end
          local tracked = prefix .. ':group:' .. token .. ':state-indexes:' .. domain
          local members = prefix .. ':group:' .. token .. ':members'
          local canonical = redis.call('ZRANGE',members,0,0)[1]
          local expected = string.rep('0',19-string.len(ARGV[16])) .. ARGV[16] .. '|' .. task_id
          if canonical ~= expected then return end
          for _, index_key in ipairs(redis.call('SMEMBERS',tracked)) do
            redis.call('ZREM', index_key, token)
          end
          redis.call('DEL',tracked)
          local task_revision = tonumber(redis.call('HGET',KEYS[3],'revision') or '-1')
          local incoming_task_revision = tonumber(ARGV[7])
          if redis.call('HGET',KEYS[3],'tombstone') ~= '0'
              or task_revision ~= incoming_task_revision then return end
          for _, index_key in ipairs(redis.call('SMEMBERS',KEYS[2])) do
            local group_index = string.gsub(index_key, ':stateidx:v1:', ':gidx-state:v1:')
            redis.call('ZADD', group_index, 1, token)
            redis.call('SADD', tracked, group_index)
          end
        end
        local state_indexes = KEYS[2]
        for _, index_key in ipairs(redis.call('SMEMBERS',state_indexes)) do
          redis.call('SREM',index_key,task_id)
        end
        redis.call('DEL',state_indexes)
        redis.call('HSET',KEYS[1],'environment',ARGV[3],'run_id',ARGV[4],
          'task_id',ARGV[5],'domain',ARGV[6],'domain_revision',ARGV[1],
          'task_revision',ARGV[7],'event_id',ARGV[2],'observed_at',ARGV[8],
          'removed',ARGV[9],'ciphertext',ARGV[10],'aad',ARGV[11],
          'nonce',ARGV[12],'ciphertext_sha256',ARGV[13],'key_version',ARGV[14])
        if ARGV[9] == '0' then
          for i = 17,#ARGV do
            redis.call('SADD',ARGV[i],task_id)
            redis.call('SADD',state_indexes,ARGV[i])
          end
        end
        if row_group ~= '' then rebuild_domain_group(row_group) end
        return 'applied'
        """;
    private static final String APPLY = """
        local old = tonumber(redis.call('HGET',KEYS[1],'revision') or '-1')
        local revision = tonumber(ARGV[1])
        if redis.call('HGET',KEYS[1],'event_id') == ARGV[2] then return 'duplicate' end
        if revision < old then return 'stale' end
        if revision == old and old >= 0 then return 'revision_conflict' end
        local prefix = ARGV[16]
        local old_group = redis.call('HGET',KEYS[1],'row_group_token') or ''
        local old_source_id = redis.call('HGET',KEYS[1],'source_id') or ''
        local old_community = redis.call('HGET',KEYS[1],'community_token') or ''
        local new_group = ARGV[17]
        local new_community = ARGV[18]
        local function group_keys(token)
          return prefix .. ':group:' .. token .. ':members', prefix .. ':group:' .. token .. ':indexes'
        end
        local function remove_group_community(token, community, task_id)
          if token == '' or community == '' then return end
          local relation = prefix .. ':group:' .. token .. ':community:' .. community
          redis.call('SREM', relation, task_id)
          if redis.call('SCARD', relation) == 0 then
            redis.call('DEL', relation)
            local community_index = prefix .. ':gidx-community:' .. community
            redis.call('ZREM', community_index, token)
            if redis.call('ZCARD', community_index) == 0 then
              redis.call('SREM', prefix .. ':community-tokens', community)
            end
          end
        end
        local function add_group_community(token, community, task_id)
          if token == '' or community == '' then return end
          local relation = prefix .. ':group:' .. token .. ':community:' .. community
          local was_empty = redis.call('SCARD', relation) == 0
          redis.call('SADD', relation, task_id)
          if was_empty then redis.call('ZADD', prefix .. ':gidx-community:' .. community, 1, token) end
          redis.call('SADD', prefix .. ':community-tokens', community)
        end
        local function rebuild_group(token)
          if token == '' then return end
          local members_key, group_indexes_key = group_keys(token)
          for _, key in ipairs(redis.call('SMEMBERS', group_indexes_key)) do redis.call('ZREM',key,token) end
          redis.call('DEL',group_indexes_key)
          local canonical = redis.call('ZRANGE',members_key,0,0)[1]
          if not canonical then return end
          for _, key in ipairs(redis.call('SMEMBERS',prefix .. ':indexes:' .. canonical)) do
            local group_index = string.gsub(key, ':idx:', ':gidx:')
            redis.call('ZADD',group_index,1,token)
            redis.call('SADD',group_indexes_key,group_index)
          end
        end
        local function rebuild_group_state(token)
          if token == '' then return end
          local domains = {'address_match','watch','qmf','residence','registration','review','task_graph'}
          for _, domain in ipairs(domains) do
            local tracked = prefix .. ':group:' .. token .. ':state-indexes:' .. domain
            for _, key in ipairs(redis.call('SMEMBERS',tracked)) do redis.call('ZREM',key,token) end
            redis.call('DEL',tracked)
          end
          local members_key = group_keys(token)
          local canonical = redis.call('ZRANGE',members_key,0,0)[1]
          if not canonical then return end
          local separator = string.find(canonical, '|', 1, true)
          if not separator then return end
          local canonical_task = string.sub(canonical, separator + 1)
          local task_key = prefix .. ':task:' .. canonical_task
          if redis.call('HGET',task_key,'tombstone') ~= '0' then return end
          local task_revision = redis.call('HGET',task_key,'revision') or '-1'
          local view_prefix = 'binhu:task-rm:v1:' .. ARGV[12] .. ':' .. ARGV[15] .. ':view-state:'
          for _, domain in ipairs(domains) do
            local tracked = prefix .. ':group:' .. token .. ':state-indexes:' .. domain
            local state_key = view_prefix .. canonical_task .. ':' .. domain .. ':record'
            if redis.call('HGET',state_key,'removed') == '0'
                and redis.call('HGET',state_key,'task_revision') == task_revision then
              local state_indexes = 'binhu:task-rm:v1:' .. ARGV[12] .. ':' .. ARGV[15]
                .. ':view-state:' .. canonical_task .. ':' .. domain .. ':index:v1:members'
              for _, index_key in ipairs(redis.call('SMEMBERS',state_indexes)) do
                if redis.call('SISMEMBER',index_key,canonical_task) == 1 then
                  local group_index = string.gsub(index_key, ':stateidx:v1:', ':gidx-state:v1:')
                  redis.call('ZADD',group_index,1,token)
                  redis.call('SADD',tracked,group_index)
                end
              end
            end
          end
        end
        for _, key in ipairs(redis.call('SMEMBERS',KEYS[2])) do redis.call('ZREM',key,ARGV[5]) end
        redis.call('DEL',KEYS[2])
        if old_group ~= '' then
          local old_members_key = group_keys(old_group)
          local old_member = string.rep('0',19-string.len(old_source_id)) .. old_source_id .. '|' .. ARGV[5]
          redis.call('ZREM',old_members_key,old_member)
          remove_group_community(old_group,old_community,ARGV[5])
          redis.call('ZREM',prefix .. ':gidx:all',old_group)
          if redis.call('ZCARD',old_members_key)>0 then redis.call('ZADD',prefix .. ':gidx:all',1,old_group) end
          rebuild_group(old_group)
          rebuild_group_state(old_group)
        end
        redis.call('HSET',KEYS[1],'environment',ARGV[12],'task_id',ARGV[13],'run_id',ARGV[15],
          'revision',ARGV[1],'event_id',ARGV[2],'command_id',ARGV[3],
          'source_id',ARGV[14],'tombstone',ARGV[4],
          'ciphertext',ARGV[7],'aad',ARGV[8],'nonce',ARGV[9],
          'ciphertext_sha256',ARGV[10],'key_version',ARGV[11],'updated_at',ARGV[6])
        if ARGV[4] == '1' then
          redis.call('HSET',KEYS[1],'tombstone','1','ciphertext','','aad','','nonce','',
            'ciphertext_sha256','','key_version','','row_group_token','','community_token','')
          local domains = {'address_match','watch','qmf','residence','registration','review','task_graph'}
          for _, domain in ipairs(domains) do
            local state_indexes = 'binhu:task-rm:v1:' .. ARGV[12] .. ':' .. ARGV[15]
              .. ':view-state:' .. ARGV[5] .. ':' .. domain .. ':index:v1:members'
            for _, index_key in ipairs(redis.call('SMEMBERS',state_indexes)) do
              redis.call('SREM',index_key,ARGV[5])
            end
            redis.call('DEL',state_indexes)
          end
        else
          redis.call('HSET',KEYS[1],'tombstone','0','row_group_token',new_group,'community_token',new_community)
          local members_key = group_keys(new_group)
          local source_member = string.rep('0',19-string.len(ARGV[14])) .. ARGV[14] .. '|' .. ARGV[5]
          redis.call('ZADD',members_key,0,source_member)
          redis.call('ZADD',prefix .. ':gidx:all',1,new_group)
          add_group_community(new_group,new_community,ARGV[5])
          for i=19,#ARGV do
            local separator=string.find(ARGV[i],'|',1,true)
            local key=string.sub(ARGV[i],1,separator-1)
            redis.call('ZADD',key,revision,ARGV[5])
            redis.call('SADD',KEYS[2],key)
          end
          rebuild_group(new_group)
          rebuild_group_state(new_group)
        end
        redis.call('INCR',KEYS[3])
        return 'applied'
        """;

    private transient JedisPooled redis;
    private transient ObjectMapper mapper;
    private transient byte[] encryptionKey;
    private transient byte[] viewStateKey;
    private transient byte[] indexKey;
    private String environment;
    private String runId;

    @Override
    public void open(Configuration parameters) throws Exception {
        environment = required("APP_ENVIRONMENT");
        runId = required(environment.equals("development") ? "DEV_RUN_ID" : "STAGING_RUN_ID");
        if (!(environment.equals("development") || environment.equals("staging"))) {
            throw new IllegalStateException("isolated event pipeline required");
        }
        if (!(environment.equals("development")
                ? runId.matches("dev-[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
                : runId.matches("STG-[0-9]{8}-[0-9]{2}"))) {
            throw new IllegalStateException("pipeline runtime identity mismatch");
        }
        String secret = required("TASK_EVENT_ENCRYPTION_KEY");
        if (secret.length() < 32) throw new IllegalStateException("task event key unavailable");
        byte[] source = secret.getBytes(StandardCharsets.UTF_8);
        encryptionKey = MessageDigest.getInstance("SHA-256").digest(join(
            "binhu/task-event/aes-gcm/v1\0".getBytes(StandardCharsets.UTF_8), source));
        viewStateKey = MessageDigest.getInstance("SHA-256").digest(join(
            "binhu/task-view-state/aes-gcm/v1\0".getBytes(StandardCharsets.UTF_8), source));
        indexKey = hmac(encryptionKey, join(
            "binhu/task-read-model-index/v1\0".getBytes(StandardCharsets.UTF_8),
            environment.getBytes(StandardCharsets.UTF_8)));
        URI redisUri = URI.create(required("BACKEND_REDIS_URL"));
        if (!"redis".equals(redisUri.getScheme()) || redisUri.getHost() == null
            || redisUri.getPort() != 6379 || !"/0".equals(redisUri.getPath())
            || !(redisUri.getHost().equalsIgnoreCase("redis")
                || redisUri.getHost().equalsIgnoreCase("environment-redis"))
            || redisUri.getHost().equalsIgnoreCase("staging-derived-redis")
            || redisUri.getHost().equalsIgnoreCase("dev-derived-redis")) {
            throw new IllegalStateException("Backend Redis identity unavailable");
        }
        redis = new JedisPooled(redisUri);
        if (!"PONG".equals(redis.ping())) throw new IllegalStateException("Redis identity unavailable");
        mapper = new ObjectMapper();
    }

    @Override
    public void invoke(Row row, Context context) throws Exception {
        int schemaVersion = ((Number) row.getField(0)).intValue();
        if (schemaVersion == 5) {
            applyViewState(row);
            return;
        }
        String eventId = string(row, 1);
        String commandId = string(row, 2);
        String eventType = string(row, 3);
        String taskId = string(row, 4);
        long sourceId = ((Number) row.getField(5)).longValue();
        long revision = ((Number) row.getField(6)).longValue();
        String operationId = string(row, 7);
        Object changedFieldsValue = row.getField(8);
        String timestamp = string(row, 9);
        String eventEnvironment = string(row, 10);
        String eventRunId = string(row, 11);
        if (!(schemaVersion == 4 || schemaVersion == 6)
            || !uuid(eventId) || !uuid(commandId) || !uuid(operationId)) {
            throw new IllegalArgumentException("task read-model event contract invalid");
        }
        if (!(changedFieldsValue instanceof List<?> changedFieldsList) || changedFieldsList.isEmpty()) {
            throw new IllegalArgumentException("task read-model changed fields missing");
        }
        Set<String> changedFields = new HashSet<>();
        for (Object value : changedFieldsList) {
            if (!(value instanceof String) || !CHANGED_FIELDS.contains(value) || !changedFields.add((String) value)) {
                throw new IllegalArgumentException("task read-model changed fields invalid");
            }
        }
        String[] taskIdentity = taskId.split(":", -1);
        if (!environment.equals(eventEnvironment) || !runId.equals(eventRunId)
            || taskIdentity.length != 2 || !SOURCE_TABLES.contains(taskIdentity[0])
            || !taskIdentity[1].matches("[1-9][0-9]{0,18}")
            || taskIdentity[1].length() == 19 && taskIdentity[1].compareTo(Long.toString(Long.MAX_VALUE)) > 0
            || sourceId < 1 || revision < 1
            || !Set.of("task.saved", "task.created", "task.deleted", "task.claimed", "task.assigned", "task.reviewed", "task.archived").contains(eventType)) {
            throw new IllegalArgumentException("task read-model identity mismatch");
        }
        if (!timestamp.matches("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]{1,6})?Z$")) {
            throw new IllegalArgumentException("task read-model timestamp invalid");
        }
        try {
            Instant.parse(timestamp);
        } catch (Exception error) {
            throw new IllegalArgumentException("task read-model timestamp invalid");
        }
        String prefix = "binhu:task-rm:v4:" + environment + ":" + runId;
        String taskKey = prefix + ":task:" + taskId;
        String indexesKey = prefix + ":indexes:" + taskId;
        String watermarkKey = prefix + ":watermark";
        boolean deleted = "task.deleted".equals(eventType) || "task.archived".equals(eventType);
        String rowGroupToken = "";
        String communityToken = "";
        String ciphertext = "", aad = "", nonce = "", digest = "", keyVersion = "";
        Set<String> indexKeys = new HashSet<>();
        if (!deleted) {
            if (schemaVersion == 6) {
                rowGroupToken = string(row, 15);
                if (!rowGroupToken.matches("[0-9a-f]{64}")) {
                    throw new IllegalArgumentException("task row group token invalid");
                }
            }
            Row envelope = (Row) row.getField(12);
            if (envelope == null) throw new IllegalArgumentException("encrypted task snapshot required");
            String algorithm = string(envelope, 0);
            keyVersion = string(envelope, 1);
            nonce = string(envelope, 2);
            ciphertext = string(envelope, 3);
            aad = string(envelope, 4);
            digest = string(envelope, 5);
            String expectedKeyVersion = schemaVersion == 6 ? "task-event-v3" : "task-event-v2";
            if (!"AES-256-GCM".equals(algorithm) || !expectedKeyVersion.equals(keyVersion)) {
                throw new IllegalArgumentException("task event encryption unsupported");
            }
            byte[] sealed = Base64.getUrlDecoder().decode(pad(ciphertext));
            if (!hex(MessageDigest.getInstance("SHA-256").digest(sealed)).equals(digest)) {
                throw new IllegalArgumentException("task event ciphertext hash mismatch");
            }
            byte[] aadBytes = Base64.getUrlDecoder().decode(pad(aad));
            String expectedAad = schemaVersion == 6
                ? "{\"command_id\":\"" + commandId
                    + "\",\"contract\":\"binhu.task-snapshot.v3\",\"environment\":\""
                    + environment + "\",\"event_id\":\"" + eventId + "\",\"revision\":"
                    + revision + ",\"row_group_token\":\"" + rowGroupToken
                    + "\",\"task_id\":\"" + taskId + "\"}"
                : "{\"command_id\":\"" + commandId
                    + "\",\"contract\":\"binhu.task-snapshot.v2\",\"environment\":\""
                    + environment + "\",\"event_id\":\"" + eventId + "\",\"revision\":"
                    + revision + ",\"task_id\":\"" + taskId + "\"}";
            if (!MessageDigest.isEqual(aadBytes, expectedAad.getBytes(StandardCharsets.UTF_8))) {
                throw new IllegalArgumentException("task event identity mismatch");
            }
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.DECRYPT_MODE, new SecretKeySpec(encryptionKey, "AES"),
                new GCMParameterSpec(128, Base64.getUrlDecoder().decode(pad(nonce))));
            cipher.updateAAD(aadBytes);
            JsonNode snapshot = mapper.readTree(cipher.doFinal(sealed));
            if (schemaVersion == 4) {
                rowGroupToken = legacyRowGroupToken(taskIdentity[0], snapshot);
            }
            JsonNode communityNode = snapshot.get("社区");
            if (taskIdentity[0].equals("t_suspect_unrevoked")) communityNode = snapshot.get("下发社区");
            String communityValue = communityNode != null && communityNode.isTextual()
                ? communityNode.asText().strip() : "";
            communityToken = token("group-community",
                communityValue.isBlank() ? "__missing_community__" : communityValue);
            snapshot.fields().forEachRemaining(entry -> {
                JsonNode value = entry.getValue();
                if (value.isTextual() && !value.asText().isBlank()) {
                    addIndex(indexKeys, prefix, entry.getKey(), value.asText());
                    String label = entry.getKey();
                    if (label.contains("社区")) addIndex(indexKeys, prefix, "community", value.asText());
                    if (label.contains("核查人") || label.contains("责任人")) addIndex(indexKeys, prefix, "inspector", value.asText());
                    addSearchGrams(indexKeys, prefix, value.asText());
                }
            });
            String parser = taskId.substring(0, taskId.indexOf(':'));
            indexKeys.add(prefix + ":idx:" + token("task_type", parser));
        }
        List<String> args = new ArrayList<>(List.of(
            Long.toString(revision), eventId, commandId, deleted ? "1" : "0", taskId,
            timestamp, ciphertext, aad, nonce, digest, keyVersion, environment, taskId,
            Long.toString(sourceId), runId));
        args.add(prefix);
        args.add(rowGroupToken);
        args.add(deleted ? "" : communityToken);
        indexKeys.stream().sorted().forEach(key -> args.add(key + "|"));
        Object result = redis.eval(APPLY, List.of(taskKey, indexesKey, watermarkKey), args);
        if ("revision_conflict".equals(result)) {
            throw new IllegalStateException("task read-model revision conflict");
        }
    }

    private void applyViewState(Row row) throws Exception {
        String eventId = string(row, 1);
        String taskId = string(row, 4);
        long sourceId = ((Number) row.getField(5)).longValue();
        long domainRevision = ((Number) row.getField(6)).longValue();
        String operationId = string(row, 7);
        String eventType = string(row, 3);
        String timestamp = string(row, 9);
        String eventEnvironment = string(row, 10);
        String eventRunId = string(row, 11);
        String domain = string(row, 13);
        long taskRevision = ((Number) row.getField(14)).longValue();
        if (!uuid(eventId) || !uuid(operationId)
            || !Set.of("task.view_state.changed", "task.view_state.removed").contains(eventType)
            || !VIEW_DOMAINS.contains(domain) || sourceId < 1 || domainRevision < 1 || taskRevision < 1
            || !environment.equals(eventEnvironment) || !runId.equals(eventRunId)) {
            throw new IllegalArgumentException("task view-state identity mismatch");
        }
        String[] taskIdentity = taskId.split(":", -1);
        if (taskIdentity.length != 2 || !SOURCE_TABLES.contains(taskIdentity[0])
            || !taskIdentity[1].matches("[1-9][0-9]{0,18}")) {
            throw new IllegalArgumentException("task view-state task identity invalid");
        }
        if (!timestamp.matches("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]{1,6})?Z$")) {
            throw new IllegalArgumentException("task view-state timestamp invalid");
        }
        Row envelope = (Row) row.getField(12);
        if (envelope == null || !"AES-256-GCM".equals(string(envelope, 0))
            || !"task-view-state-v1".equals(string(envelope, 1))) {
            throw new IllegalArgumentException("task view-state encryption unsupported");
        }
        String nonce = string(envelope, 2);
        String ciphertext = string(envelope, 3);
        String aad = string(envelope, 4);
        String digest = string(envelope, 5);
        byte[] sealed = Base64.getUrlDecoder().decode(pad(ciphertext));
        byte[] aadBytes = Base64.getUrlDecoder().decode(pad(aad));
        String expectedAad = "{\"contract\":\"binhu.task-view-state.v1\",\"domain\":\"" + domain
            + "\",\"domain_revision\":" + domainRevision + ",\"environment\":\"" + environment
            + "\",\"event_id\":\"" + eventId + "\",\"run_id\":\"" + runId
            + "\",\"task_id\":\"" + taskId + "\",\"task_revision\":" + taskRevision + "}";
        if (!hex(MessageDigest.getInstance("SHA-256").digest(sealed)).equals(digest)
            || !MessageDigest.isEqual(aadBytes, expectedAad.getBytes(StandardCharsets.UTF_8))) {
            throw new IllegalArgumentException("task view-state payload identity mismatch");
        }
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.DECRYPT_MODE, new SecretKeySpec(viewStateKey, "AES"),
            new GCMParameterSpec(128, Base64.getUrlDecoder().decode(pad(nonce))));
        cipher.updateAAD(aadBytes);
        JsonNode state = mapper.readTree(cipher.doFinal(sealed));
        if (!state.isObject() || ("task.view_state.removed".equals(eventType) && !state.isEmpty())
            || ("task.view_state.changed".equals(eventType) && state.isEmpty())) {
            throw new IllegalArgumentException("task view-state payload invalid");
        }
        String key = "binhu:task-rm:v1:" + environment + ":" + runId
            + ":view-state:" + taskId + ":" + domain + ":record";
        String prefix = "binhu:task-rm:v4:" + environment + ":" + runId;
        String membershipKey = "binhu:task-rm:v1:" + environment + ":" + runId
            + ":view-state:" + taskId + ":" + domain + ":index:v1:members";
        String taskKey = prefix + ":task:" + taskId;
        Set<String> stateIndexes = stateIndexes(domain, state);
        List<String> args = new ArrayList<>(List.of(
            Long.toString(domainRevision), eventId, environment, runId, taskId, domain,
            Long.toString(taskRevision), timestamp,
            "task.view_state.removed".equals(eventType) ? "1" : "0",
            ciphertext, aad, nonce, digest, "task-view-state-v1"));
        args.add(prefix);
        args.add(Long.toString(sourceId));
        stateIndexes.stream().sorted().forEach(args::add);
        Object result = redis.eval(APPLY_VIEW_STATE, List.of(key, membershipKey, taskKey), args);
        if ("revision_conflict".equals(result)) {
            throw new IllegalStateException("task view-state revision conflict");
        }
    }

    @Override
    public void close() {
        if (redis != null) redis.close();
    }

    private String token(String field, String value) {
        String normalized = value.trim().replaceAll("[ \\t\\n\\x0B\\f\\r]+", " ").toLowerCase(Locale.ROOT);
        return hex(hmac(indexKey, join(field.getBytes(StandardCharsets.UTF_8),
            new byte[]{0}, normalized.getBytes(StandardCharsets.UTF_8))));
    }

    private Set<String> stateIndexes(String domain, JsonNode state) {
        Set<String> result = new HashSet<>();
        String stateField = switch (domain) {
            case "address_match", "registration" -> "status";
            case "qmf", "residence", "review", "task_graph" -> "state";
            default -> "";
        };
        JsonNode enumNode = stateField.isEmpty() ? null : state.path(stateField);
        if (enumNode != null && enumNode.isTextual()) {
            String value = enumNode.asText().strip().toLowerCase(Locale.ROOT);
            if (STATE_ENUMS.getOrDefault(domain, Set.of()).contains(value)) {
                result.add(stateIndexKey(domain, stateField, value));
            }
        }
        if (domain.equals("watch")) {
            JsonNode marks = state.path("watch_marks");
            if (marks.isArray()) {
                for (JsonNode mark : marks) {
                    JsonNode id = mark.path("category_id");
                    if (id.canConvertToInt() && id.asInt() > 0) {
                        String value = Integer.toString(id.asInt());
                        result.add(stateIndexKey(domain, "category_id", value));
                    }
                }
            }
        }
        if (domain.equals("address_match")) {
            JsonNode candidate = state.path("candidate");
            JsonNode name = candidate.path("name");
            if (!name.isTextual() || name.asText().isBlank()) name = candidate.path("community_name");
            if (name.isTextual() && !name.asText().isBlank() && name.asText().length() <= 300) {
                String value = name.asText().strip().replaceAll("(?U)\\s+", " ");
                result.add(stateIndexKey(domain, "small_community_name", value));
            }
        }
        if (domain.equals("task_graph") && state.path("blocked").isBoolean()) {
            result.add(stateIndexKey(domain, "blocked", Boolean.toString(state.path("blocked").asBoolean())));
        }
        return result;
    }

    private String stateIndexKey(String domain, String field, String value) {
        String digest = token("state:" + domain + ":" + field, value);
        return "binhu:task-rm:v4:" + environment + ":" + runId
            + ":stateidx:v1:" + digest;
    }

    private String legacyRowGroupToken(String table, JsonNode snapshot) {
        String parser;
        List<String> fields;
        switch (table) {
            case "t_fullchain" -> { parser = "全链条"; fields = List.of("身份证号", "电话号码", "下发日期"); }
            case "t_rental_check" -> { parser = "出租房屋核查"; fields = List.of("身份证号", "手机号码"); }
            case "t_police_stats" -> {
                parser = "涉警统计";
                String caseNumber = text(snapshot, "序号");
                if (caseNumber.length() == 20 && caseNumber.chars().allMatch(Character::isDigit)) {
                    return groupToken(parser, md5(caseNumber));
                }
                fields = List.of("序号", "日期", "社区", "简要警情及处理结果");
            }
            case "t_suspect_unrevoked" -> { parser = "疑似未注销模型三"; fields = List.of("身份证号", "联系方式"); }
            case "t_suspect_return" -> { parser = "疑似返苏"; fields = List.of("身份证号码", "联系号码"); }
            case "t_delivery_industry" -> { parser = "寄递业"; fields = List.of("身份证号", "手机号码"); }
            case "t_group_rental" -> { parser = "群租房核查"; fields = List.of("出租屋编号", "社区"); }
            case "t_suzhou_police" -> {
                parser = "苏州涉警";
                String caseNumber = text(snapshot, "接警编号");
                if (!caseNumber.isEmpty()) return groupToken(parser, md5(caseNumber));
                fields = List.of("身份证号", "联系号码", "下发日期");
            }
            case "t_traffic_police" -> { parser = "交通涉警"; fields = List.of("身份证号", "联系号码", "下发日期"); }
            default -> throw new IllegalArgumentException("legacy task parser unavailable");
        }
        StringBuilder key = new StringBuilder();
        for (String field : fields) {
            if (key.length() > 0) key.append('|');
            key.append(text(snapshot, field));
        }
        return groupToken(parser, md5(key.toString()));
    }

    private String groupToken(String parser, String rowKey) {
        byte[] groupKey = hmac(encryptionKey, join(
            "binhu/task-read-model/group-key/v1\0".getBytes(StandardCharsets.UTF_8),
            environment.getBytes(StandardCharsets.UTF_8)));
        return hex(hmac(groupKey, join(parser.getBytes(StandardCharsets.UTF_8),
            new byte[]{0}, rowKey.getBytes(StandardCharsets.UTF_8))));
    }

    private String md5(String value) {
        try {
            return hex(MessageDigest.getInstance("MD5").digest(value.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception error) {
            throw new IllegalStateException("task row key failed");
        }
    }

    private static String text(JsonNode snapshot, String field) {
        JsonNode value = snapshot.get(field);
        return value == null || value.isNull() ? "" : value.asText();
    }

    private void addIndex(Set<String> indexes, String prefix, String field, String value) {
        if (field.isBlank() || field.length() > 120 || value.length() > 512) return;
        indexes.add(prefix + ":idx:" + token(field, value));
    }

    private void addSearchGrams(Set<String> indexes, String prefix, String value) {
        int[] points = value.trim().replaceAll("[ \\t\\n\\x0B\\f\\r]+", " ").toLowerCase(Locale.ROOT).codePoints().toArray();
        if (points.length < 2 || points.length > 512) return;
        Set<String> grams = new HashSet<>();
        for (int i = 0; i + 1 < points.length; i++) {
            grams.add(new String(points, i, 2));
        }
        for (String gram : grams) indexes.add(prefix + ":idx:" + token("search", gram));
    }

    private static byte[] hmac(byte[] key, byte[] value) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(key, "HmacSHA256"));
            return mac.doFinal(value);
        } catch (Exception error) {
            throw new IllegalStateException("task index HMAC failed");
        }
    }

    private static String string(Row row, int index) {
        Object value = row.getField(index);
        return value == null ? "" : value.toString();
    }

    private static String required(String name) {
        String value = System.getenv(name);
        if (value == null || value.isBlank()) throw new IllegalStateException("pipeline runtime identity unavailable");
        return value;
    }

    private static byte[] join(byte[]... parts) {
        int size = 0;
        for (byte[] part : parts) size += part.length;
        byte[] out = new byte[size];
        int offset = 0;
        for (byte[] part : parts) {
            System.arraycopy(part, 0, out, offset, part.length);
            offset += part.length;
        }
        return out;
    }

    private static String hex(byte[] bytes) {
        StringBuilder out = new StringBuilder(bytes.length * 2);
        for (byte value : bytes) out.append(String.format("%02x", value));
        return out.toString();
    }

    private static String pad(String value) {
        return value + "=".repeat((4 - value.length() % 4) % 4);
    }

    private static boolean uuid(String value) {
        return value.matches("[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}");
    }
}
