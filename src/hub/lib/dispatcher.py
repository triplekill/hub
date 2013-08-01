#!/usr/bin/env python
'''
This is the Hub dispatcher which performs job management functions
'''
# core modules
import sys
import uuid
import logging
import traceback
import time
import threading
import zmq

# own modules
import hub.lib.error as error
import hub.lib.config as config
from hub.lib.jobs import Job
from hub.lib.tasks import Task
from hub.lib.common import Daemon

# 3rd party modules
import pika
import json


class DispatcherDaemon(Daemon):
    '''
    Subclass of Daemon class with run method to launch dispatcher
    '''
    def run(self, *args):
        self.log = logging.getLogger(__name__)
        broker = args[0]
        try:
            Dispatcher().start(broker)
        except Exception, e:
            self.log.exception(e)


class Dispatcher():
    '''
    Class representing dispatcher that performs job management functions
    '''
    def __init__(self):
        '''
        Setup connection to broker and listen for incoming jobs and results
        '''
        self.log = logging.getLogger(__name__)
        self.registered_jobs = {}
        # Setup config
        try:
            self.conf = config.setup('/usr/local/pkg/hub/etc/dispatcher.conf')
        except error.ConfigError, e:
            print e.msg
            raise e
        self.databaseType = self.conf.get('DATABASE','type')
        self.databaseHost = self.conf.get('DATABASE','host')
        self.databasePort = self.conf.get('DATABASE','port')
        self.databaseInstance = self.conf.get('DATABASE','instance')
        
        self.databaseModule = __import__('hub.lib.database',fromlist = [self.databaseType])
        self.db = getattr(self.databaseModule, self.databaseType)
        self.ct_lock = threading.Lock()

    def _caretaker(self):
        self.log.info("Caretaker waiting on lock...")
        #TODO: Make this check the task that triggered it first, then cleanup
        self.ct_lock.acquire()
        self.log.info("Caretaker Running...")
        dbI=self.db(self.databaseHost,self.databasePort,self.databaseInstance)
        incomplete = dbI.getincompletetasks()
        for task_id in incomplete:
            jobid = dbI.getjobid(task_id)
            jobrecord = dbI.getjob(jobid)
            #TODO get task record not job record
            job = Job().load(jobrecord)
            for task in job.state.tasks:
                if task.state.id == task_id and task.state.timeout and task.state.start_time:
                    if task.state.timeout < (time.time() - task.state.start_time):
                        self.log.info("Setting task {0} from job {1} as FAILED".format(task.state.id,job.state.id))
                        task.state.status = 'FAILED'
                        task.state.end_time = time.time()
                        job.state.status = 'FAILED'
                        job.state.end_time = time.time()                        
                        job.save()
                        dbI.updatejob(job)
        self.ct_lock.release()            

    def _persist_job(self, job):
        
        self.db(self.databaseHost,self.databasePort,self.databaseInstance).putjob(job)
        
    def _update_job(self, job):
        self.db(self.databaseHost,self.databasePort,self.databaseInstance).updatejob(job)
        
    def _retreive_job(self, job_id):
        
        job = self.db(self.databaseHost,self.databasePort,self.databaseInstance).getjob(job_id)
        return job
    
    def _retreive_jobid(self, task_id):
        jobid = self.db(self.databaseHost,self.databasePort,self.databaseInstance).getjobid(task_id)
        return jobid

    def work_q(self):
        self.newcontext = zmq.Context()
        self.task_q = self.context.socket(zmq.DEALER)
        self.workers = self.newcontext.socket(zmq.PUB)
        self.workers.bind("tcp://*:5561")
        self.task_q.setsockopt(zmq.IDENTITY, "TASK_Q")
        self.task_q.connect("tcp://localhost:5560")
        
        self.qpoller = zmq.Poller()
        self.qpoller.register(self.task_q, zmq.POLLIN)
        
        self.workers_addr = []
        time.sleep(1)
        self.log.debug("Tell the workers to call home...")
        self.workers.send_multipart(['CALL_HOME', 'DISPATCHER_STARTED'])
        while True:
            self.qsocks = dict(self.qpoller.poll())
            if self.workers_addr:
                if self.qsocks.get(self.task_q) == zmq.POLLIN:
                    blank = self.task_q.recv()
                    message = self.task_q.recv()
                    work_addr = self.workers_addr.pop()
                    self.log.info("Task {0} is being sent to worker {1}".format(message, work_addr))
                    try:
                        self.workers.send_multipart([work_addr.encode(), message])
                    except Exception as e:
                        self.log.error(e)
                    self.log.info("SENT")

#            if self.qsocks.get(self.backend) == zmq.POLLIN:
#                addr = self.backend.recv()                    
#                empty = self.backend.recv()
#                message = self.backend.recv()
#                if message == "READY":
#                    self.log.info("Worker {0} is READY".format(addr))
#                    self.workers+=1
#                    self.workers_addr.append(addr)
        
    def start(self, broker):
        self.log.info('Starting dispatcher, listening for jobs and results...')
        self.context = zmq.Context()
        self.status = self.context.socket(zmq.ROUTER)
        self.job_queue = self.context.socket(zmq.ROUTER)
        self.status.bind("tcp://*:5559")
        self.job_queue.bind("tcp://*:5560")
        
        
        # Initialize poll set
        self.poller = zmq.Poller()
        self.poller.register(self.status, zmq.POLLIN)
        self.poller.register(self.job_queue, zmq.POLLIN)
        qthread = threading.Thread(target=self.work_q)
        qthread.start()
        # Switch messages between sockets
        msgs = []
        while True:
            self.socks = dict(self.poller.poll())
            if self.socks.get(self.job_queue) == zmq.POLLIN:
                message = self.job_queue.recv()
                more = self.job_queue.getsockopt(zmq.RCVMORE)
                if more:
                    msgs.append(message)
                else:
                    incoming = json.loads(message)
                    to_reply = []
                    to_publish = []
                    if incoming['key'] == 'task_update':
                        task = incoming['data']
                        to_publish = self.process_results(json.dumps(task), fromWorker=False)
                    elif incoming['key'] == 'task_result':
                        task = incoming['data']
                        to_publish = self.process_results(json.dumps(task), fromWorker=True)
                    elif incoming['key'] == 'job':
                        job = incoming['data']
                        result = self.process_jobs(json.dumps(job))
                        to_publish = result[0]
                        to_reply = [result[1]]
                    elif incoming['key'] == 'announce':
                        worker_id = incoming['data']
                        self.log.info("Worker {0} is READY".format(worker_id))
                        self.workers_addr.append(worker_id)
                    for reply in to_reply:
                        for msg in msgs:
                            self.job_queue.send(msg, zmq.SNDMORE)   
                        self.job_queue.send(reply)        
                    for publish in to_publish:
                        self.job_queue.send("TASK_Q",zmq.SNDMORE)
                        self.job_queue.send("",zmq.SNDMORE)
                        self.job_queue.send(publish)
                        
                    msgs = []
                        


            if self.socks.get(self.status) == zmq.POLLIN:
                message = self.status.recv()
                more = self.status.getsockopt(zmq.RCVMORE)
                if more:
                    msgs.append(message)
                else:
                    incoming = json.loads(message)
                    to_reply = []
                    if incoming['key'] == 'status':
                        #Do something to find the job resulting in...
                        jobid = incoming['data']['id']
                        job = self.get_job(jobid)
                        to_reply = [job]
                        for reply in to_reply:
                            for msg in msgs:
                                self.status.send(msg, zmq.SNDMORE)   
                            self.status.send(reply)
                    msgs = []                  

    def _start_next_task(self, job):
        tasks_to_run = job.get_next_tasks_to_run()
        if len(tasks_to_run) == 0:
            # We're done, calculate overall job status and exit
            job.set_status()
            if job.state.status == 'SUCCESS':
                job.state.end_time = time.time()
                job.update_output()
            self.log.info('No more tasks to run for job {0}'.format(
                job.state.name))
            self.log.debug('Updating job {0} in DB'.format(
                job.state.name))
            self._update_job(job)
            self.log.info('Job {0} completed. Status: {1}, Output: {2}'.format(
                          job.state.id, job.state.status, job.state.output))
#            self.backend.send("None")
        ret = []
        for task in tasks_to_run:
            # Sub tagged inputs with the associated results of completed tasks
            if task.state.status != 'RUNNING' and task.state.args is not None:
                task = job.update_task_args(task)
            task.state.status = 'SUBMITTED'
            if not task.state.start_time:
                task.state.start_time = time.time()
            if task.state.timeout:
                self.log.debug("Task {0} timeout in {1}".format(task.state.id,str(task.state.timeout)))
                threading.Timer(task.state.timeout, self._caretaker).start()
            ret.append(task.state.save())
            #Now we've decided what to do NEXT with the Job lets update the DB
            self.log.debug("Updating to DB job: ".format(job.state.id))
            self._update_job(job)
        return ret
        

    def get_job(self, jobid):
        '''
        Work out dependancies and order
        '''
        self.log.info('Received status request for job {0}'.format(jobid))
        # Get the job from the store of registered jobs
        jobs = dict()
        if jobid == 'all':
            for id, job in self.registered_jobs.iteritems():
                jobs[id] = str(job.save())
            msg = str(jobs)
        else:
            job = self._retreive_job(jobid)
            if job is not None:
                msg = job
            else:
                msg = str('Job %s not found' % jobid)
        # Return job to client
        return msg

    def process_jobs(self, jobrecord):
        '''
        Work out dependancies and order
        '''
        ret = []
        # Create a Job instance from the job record
        job = Job().load(jobrecord)
        self._persist_job(job)
        self.log.info('Registered job: {0} in DB'.format(job.state.id))
        # Work out the first tasks to run
        self.log.debug('Decomposing job; calculating first tasks to run')
        tasks_to_run = job.get_next_tasks_to_run()
        #This was added to fill out any id args in tasks right at the beginning
        for task in tasks_to_run:
            if task.state.args is not None:
                task = job.update_task_args(task)
        for task in tasks_to_run:
            task.state.status = 'SUBMITTED'
            if not task.state.start_time:
                task.state.start_time = time.time()
            if task.state.timeout:
                self.log.debug("Task {0} timeout in {1}".format(task.state.id,str(task.state.timeout)))
                threading.Timer(task.state.timeout, self._caretaker).start()
            ret.append(task.state.save())
        #Now we've decided what to do with Job lets update the DB
        self.log.debug("Updating to DB job: {0}".format(job.state.id))
        self._update_job(job)
        return (ret, json.dumps(job.state.id))

    def process_results(self, taskrecord, fromWorker=False):
        '''
        Processing results received from workers and end points
        '''
        started_tasks = []
        # Check if task is registered to this dispatcher
        if fromWorker:
            jobid = json.loads(taskrecord)['parent_id']
            self.log.info(
            'Received task results for job {0}'.format(
                jobid))
            jobrecord = self._retreive_job(jobid)
            if jobrecord is None:
                self.log.warn('No parent job found with id {0}'.format(
                                      jobid))
            else:
                job = Job().load(jobrecord)
                self.log.info('Found job in DB: {0}'.format(job.state.id))
                self.log.info('Task results: {0}'.format(taskrecord))
                # Turn the taskrecord into a project Task instance
                updated_task = Task().load(taskrecord)            
                # Update the job with the new task results
                job.update_tasks(updated_task, force=True)
                started_tasks = self._start_next_task(job)
        else:
            self.log.info('Task results: {0}'.format(taskrecord))
            # Turn the taskrecord into a project Task instance
            updated_task = Task().load(taskrecord)
            number_of_updated_tasks = 0
            jobid = self._retreive_jobid(updated_task.state.id)
            jobrecord = self._retreive_job(jobid)
            if jobrecord is not None:
                job = Job().load(jobrecord)
                self.log.info('Found in DB job: {0}'.format(job.state.id))
                for task in job.state.tasks:
                    if updated_task.state.id == task.state.id:
                        job.update_tasks(updated_task)
                        number_of_updated_tasks += 1
                        started_tasks = self._start_next_task(job)
                if number_of_updated_tasks == 0:
                    self.log.warn('Task with id {0} not found in its parent job (possible?)'.format(
                                  updated_task.state.id))
            else:
                self.log.warn('No parent job found for Task with id {0}'.format(
                                      updated_task.state.id))
       
        return started_tasks

if __name__ == '__main__':
    '''
    Run dispatcher directly by executing this module, passing the broker
    hostname/IP as the only argument.
    '''
    try:
        Dispatcher().start(sys.argv[1])
    except Exception, e:
        print(e)
