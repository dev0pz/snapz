import boto3
import time
import datetime
import os
import json
from kubernetes import client, config
from kubernetes.client.rest import ApiException
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SERVICE_TOKEN_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/token"
SERVICE_CERT_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
KUBERNETES_HOST = "https://%s:%s" % (os.getenv("KUBERNETES_SERVICE_HOST"), os.getenv("KUBERNETES_SERVICE_PORT"))

debug_on = False

a_data = {'AWS_ACCESS_KEY_ID':'xxx', 'AWS_SECRET_ACCESS_KEY':'xxx', 'AWS_DEFAULT_REGION':'xxx', 'SNAPSHOT_OWNER_ID':'xxx', 'VOLUME_AVAILABILITY_ZONE':'xxx', 'DEBUG':False , 'SNAPSHOT_OLDER_THAN_N_DAYS':7, 'AWS_VOLUME_SIZE':2 , 'AWS_VOLUME_TYPE': 'gp2', 'K8S_PV_NAME': 'mongo-pv', 'K8S_PV_SIZE':'2Gi'}

try:
    os.environ['DEBUG']
except KeyError:
    debug_on = False
else:
    debug_on = True

for k, v in a_data.items():
    try:
        os.environ[k]
    except KeyError:
        print "no env var for",k,a_data[k]
    else:
        if (k == 'SNAPSHOT_OLDER_THAN_N_DAYS' or k == 'AWS_VOLUME_SIZE'):
            try:
                int(os.environ[k])
            except ValueError:
                print "error value for",k
            else:
                a_data[k] = int(os.environ[k])
        else:
            a_data[k] = os.environ[k]

## kubernetes client config
configuration = client.Configuration()
configuration.host = KUBERNETES_HOST
if not os.path.isfile(SERVICE_TOKEN_FILE):
    raise ApiException("ServiceAccount token file does not exists.")
with open(SERVICE_TOKEN_FILE) as f:
    token = f.read()
    if not token:
        raise ApiException("Token file exists but empty.")
    configuration.api_key_prefix['authorization'] = 'Bearer'
    configuration.api_key['authorization'] = token.strip('\n')
if not os.path.isfile(SERVICE_CERT_FILE):
    raise ApiException("Service certification file does not exists.")
with open(SERVICE_CERT_FILE) as f:
    if not f.read():
        raise ApiException("Cert file exists but empty.")
    configuration.ssl_ca_cert = SERVICE_CERT_FILE
client.Configuration.set_default(configuration)

v1 = client.CoreV1Api()
##

epochtimestamp = time.time()
DATEFORMAT = '%Y-%m-%d %H:%M:%S+00:00'
logtimestamp = datetime.datetime.utcfromtimestamp(epochtimestamp).strftime(DATEFORMAT)

session = boto3.Session(
aws_access_key_id = a_data['AWS_ACCESS_KEY_ID'],
aws_secret_access_key = a_data['AWS_SECRET_ACCESS_KEY']
)

ec2 = session.resource('ec2', region_name = a_data['AWS_DEFAULT_REGION'])
mlab_snapshots = ec2.snapshots.filter(OwnerIds=[a_data['SNAPSHOT_OWNER_ID'],],)
week_ago = datetime.datetime.utcnow() - datetime.timedelta(days=a_data['SNAPSHOT_OLDER_THAN_N_DAYS'])

last_snaps = [snapshot for snapshot in mlab_snapshots if datetime.datetime.strptime(str(snapshot.start_time), DATEFORMAT) >= week_ago]
last_sorted_snaps = sorted(last_snaps, key=lambda snapshot: snapshot.start_time)

try:
    last_sorted_snaps[-1]
except IndexError:
    if debug_on: print epochtimestamp,logtimestamp,"no new snapshot available"
else:
    last_snap = last_sorted_snaps[-1]
    snapID = last_snap.id
    try:
        snapID
    except NameError:
        if debug_on: print epochtimestamp,logtimestamp,"no snapshot"
    else:
        if debug_on: print epochtimestamp,logtimestamp,"last found snapshot is ", snapID, last_snap.start_time
        volumes = ec2.volumes.all()

        alreadyfromsnap = False
        for volume in volumes:
            if (volume.snapshot_id == snapID):
                alreadyfromsnap = True
                if debug_on: print epochtimestamp,logtimestamp,"found volume already created with last snapshot ", volume.id
            else:
                if debug_on: print epochtimestamp,logtimestamp,"Looking at volume ",volume.id, volume.create_time, volume.snapshot_id

        if not alreadyfromsnap:
            new_volume = ec2.create_volume(SnapshotId=snapID, AvailabilityZone=a_data['VOLUME_AVAILABILITY_ZONE'], VolumeType=a_data['AWS_VOLUME_TYPE'], Size=a_data['AWS_VOLUME_SIZE'])
            try:
                new_volume.id
            except NameError:
                if debug_on: print epochtimestamp,logtimestamp,"couldnt create a new volume from snapshot ", snapID
                print json.dumps({'success': False, 'epoch_ts': epochtimestamp, 'timestamp': logtimestamp, 'snapshot_data': {'snapshot_id': snapID, 'volumen_id': last_snap.volume_id, 'start_time': last_snap.start_time, 'progress': last_snap.progress, 'state': last_snap.state }, 'new_volume_from_snap': 'none'}, sort_keys=True, default=str)
            else:
                newvolumeid = new_volume.id
                if debug_on: print epochtimestamp,logtimestamp, "New created volume from snapshot:",newvolumeid
                print json.dumps({'success': True, 'epoch_ts': epochtimestamp, 'timestamp': logtimestamp, 'snapshot_data': {'snapshot_id': snapID, 'volumen_id': last_snap.volume_id, 'start_time': last_snap.start_time, 'progress': last_snap.progress, 'state': last_snap.state }, 'new_volume_from_snap': newvolumeid}, sort_keys=True, default=str)

                pv_manifest = {
                    'apiVersion': 'v1',
                        'kind': 'PersistentVolume',
                            'metadata': {
                                'name': a_data['K8S_PV_NAME'],
                                    'labels': {
                                        'type': 'amazonEBS',
                                        'db': 'mongo'
                                    }
                            },
                            'spec': {
                                    'capacity': {'storage': a_data['K8S_PV_SIZE']},
                                        'storageClassName': 'default',
                                            'accessModes': ['ReadWriteOnce'],
                                            'awsElasticBlockStore': {
                                                'volumeID': newvolumeid,
                                                'fsType': 'ext4'
                                            }
                             }
                }

                body = pv_manifest
                pretty = True

                deleteopt = client.V1DeleteOptions(grace_period_seconds=0)
                try:
                    del_vol = v1.delete_persistent_volume(a_data['K8S_PV_NAME'], deleteopt, pretty=pretty)
                    print(del_vol)
                except ApiException as e:
                    print("Exception when calling CoreV1Api->delete_persistent_volume: %s\n" % e)

                time.sleep(90)

                try:
                    create_vol = v1.create_persistent_volume(body, pretty=pretty)
                    print(create_vol)
                except ApiException as e:
                    print("Exception when calling CoreV1Api->create_persistent_volume: %s\n" % e)



