package binhu.shadow.flink;
import org.apache.flink.shaded.jackson2.com.fasterxml.jackson.databind.ObjectMapper;
import org.apache.flink.shaded.jackson2.com.fasterxml.jackson.databind.node.ObjectNode;

/** Executed in the pinned Flink runtime, no external test libraries or data. */
public final class MetadataContractCheck {
    public static void main(String[] args) throws Exception {
        String run="KSHADOW-20260906T084957Z-fcbad2";
        String valid="{\"schema_version\":1,\"event_id\":\"00000000-0000-0000-0000-000000000001\","
          +"\"event_type\":\"task.saved\",\"task_id\":\"t_fullchain:9\",\"source_id\":9,\"revision\":9223372036854775807,"
          +"\"operation_id\":\"00000000-0000-0000-0000-000000000002\",\"changed_fields\":[\"task_state\"],"
          +"\"timestamp\":\"2026-09-07T00:00:00Z\",\"environment\":\"shadow\",\"run_id\":\""+run+"\"}";
        if(TaskEventCheckpointJob.parse(valid,run).revision!=Long.MAX_VALUE) throw new AssertionError();
        ObjectMapper mapper=new ObjectMapper(); int count=1;
        String[][] changes={{"revision","1.5"},{"revision","true"},{"revision","9223372036854775808"},
          {"source_id","0"},{"task_id","\"t_unknown:9\""},{"run_id","\"KSHADOW-other\""},
          {"timestamp","\"2026-02-30T00:00:00Z\""},{"changed_fields","[\"phone\"]"},
          {"changed_fields","[\"task_state\",\"task_state\"]"},{"environment","\"production\""},
          {"body","\"synthetic forbidden body\""},{"task_id","\"t_fullchain:9223372036854775808\""}};
        for(String[] change:changes){
            ObjectNode node=(ObjectNode)mapper.readTree(valid); node.set(change[0],mapper.readTree(change[1]));
            reject(node.toString(),run);count++;
        }
        reject(valid+" {}",run);count++;
        reject(valid.replace("{\"schema_version\":1,","{\"schema_version\":1,\"schema_version\":1,"),run);count++;
        ObjectNode missing=(ObjectNode)mapper.readTree(valid);missing.remove("operation_id");reject(missing.toString(),run);count++;
        System.out.println("metadata_contract_passed="+count);
    }
    private static void reject(String json,String run){
        try{TaskEventCheckpointJob.parse(json,run);throw new AssertionError("invalid metadata accepted");}
        catch(IllegalArgumentException e){
            if(!"invalid shadow metadata envelope".equals(e.getMessage())||e.getCause()!=null)
                throw new AssertionError("unsafe error details");
        }
    }
}
