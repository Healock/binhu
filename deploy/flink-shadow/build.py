"""Offline, digest-pinned Flink job builder for the isolated shadow host."""
from __future__ import annotations
import hashlib,json,os,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parent
LOCK=json.loads((ROOT/'dependencies.lock.json').read_text(encoding='utf-8'))
IMAGE=f"{LOCK['flink_image']['repository']}@{LOCK['flink_image']['digest']}"

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def main():
    jar=ROOT/'dependencies'/ 'flink-sql-connector-kafka-3.3.0-1.20.jar'
    if not jar.exists() or sha(jar)!=LOCK['artifacts'][0]['sha256']: raise SystemExit('connector digest mismatch')
    subprocess.run(['docker','image','inspect',IMAGE],check=True,stdout=subprocess.DEVNULL)
    source_hash=hashlib.sha256(b''.join((ROOT/n).read_bytes() for n in ['TaskEventCheckpointJob.java','MetadataContractCheck.java'])).hexdigest()[:16]
    out=ROOT/'target-candidates'/source_hash; classes=out/'classes'; out.mkdir(parents=True,exist_ok=False); classes.mkdir(); os.chmod(classes,0o777)
    cp=' /work/dependencies/flink-sql-connector-kafka-3.3.0-1.20.jar'; shell='cp='+repr('/work/dependencies/flink-sql-connector-kafka-3.3.0-1.20.jar')+'; for j in /opt/flink/lib/*.jar; do cp="$cp:$j"; done; exec java -jar /work/ecj-3.38.0.jar -17 -cp "$cp" -d /work/classes /work/TaskEventCheckpointJob.java /work/MetadataContractCheck.java'
    subprocess.run(['docker','run','--rm','--pull=never','--network','none','--read-only','--tmpfs','/tmp:size=64m','--memory','512m','--cpus','1','-v',f'{ROOT}:/work:ro','-v',f'{classes}:/work/classes','--entrypoint','sh',IMAGE,'-c',shell],check=True)
    job=out/'flink-kafka-checkpoint-smoke.jar'
    with zipfile.ZipFile(job,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('META-INF/MANIFEST.MF','Manifest-Version: 1.0\nMain-Class: binhu.shadow.flink.TaskEventCheckpointJob\n\n')
        for f in classes.rglob('*.class'): z.write(f,f.relative_to(classes).as_posix())
    manifest={'source_hash':source_hash,'image':IMAGE,'connector_sha256':sha(jar),'job_sha256':sha(job),'output':str(job)}
    (out/'build-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    shutil.rmtree(classes)
    print(json.dumps(manifest))
if __name__=='__main__': main()
