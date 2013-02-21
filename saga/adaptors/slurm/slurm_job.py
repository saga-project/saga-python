""" SLURM job adaptor implementation """

import saga.utils.which
import saga.utils.pty_shell

import saga.adaptors.cpi.base
import saga.adaptors.cpi.job

import re
import os
import time
import textwrap
import string
SYNC_CALL  = saga.adaptors.cpi.decorators.SYNC_CALL
ASYNC_CALL = saga.adaptors.cpi.decorators.ASYNC_CALL

# --------------------------------------------------------------------
# some private defs
#
_PTY_TIMEOUT = 2.0

# --------------------------------------------------------------------
# the adaptor name
#
_ADAPTOR_NAME          = "saga.adaptor.slurm_job"
_ADAPTOR_SCHEMAS       = ["slurm", "slurm+ssh", "slurm+gsissh"]
_ADAPTOR_OPTIONS       = []

# --------------------------------------------------------------------
# the adaptor capabilities & supported attributes
#
# TODO: FILL ALL IN FOR SLURM
_ADAPTOR_CAPABILITIES  = {
    "jdes_attributes"  : [saga.job.NAME, 
                          saga.job.EXECUTABLE,
                          saga.job.ARGUMENTS,
                          saga.job.ENVIRONMENT,
                          saga.job.SPMD_VARIATION,
                          saga.job.TOTAL_CPU_COUNT, 
                          saga.job.NUMBER_OF_PROCESSES,
                          saga.job.PROCESSES_PER_HOST,
                          saga.job.THREADS_PER_PROCESS, 
                          saga.job.WORKING_DIRECTORY,
                          #saga.job.INTERACTIVE,
                          saga.job.INPUT,
                          saga.job.OUTPUT, 
                          saga.job.ERROR,
                          saga.job.FILE_TRANSFER,
                          saga.job.CLEANUP,
                          saga.job.JOB_START_TIME,
                          saga.job.WALL_TIME_LIMIT, 
                          saga.job.TOTAL_PHYSICAL_MEMORY, 
                          #saga.job.CPU_ARCHITECTURE, 
                          #saga.job.OPERATING_SYSTEM_TYPE, 
                          #saga.job.CANDIDATE_HOSTS,
                          saga.job.QUEUE,
                          saga.job.PROJECT,
                          saga.job.JOB_CONTACT],

    "job_attributes"   : [saga.job.EXIT_CODE,
                          saga.job.EXECUTION_HOSTS,
                          saga.job.CREATED,
                          saga.job.STARTED,
                          saga.job.FINISHED],
    "metrics"          : [saga.job.STATE, 
                          saga.job.STATE_DETAIL],
    "contexts"         : {"ssh"      : "public/private keypair",
                          "x509"     : "X509 proxy for gsissh",
                          "userpass" : "username/password pair for simple ssh"}
}

# --------------------------------------------------------------------
# the adaptor documentation
#
_ADAPTOR_DOC           = {
    "name"             : _ADAPTOR_NAME,
    "cfg_options"      : _ADAPTOR_OPTIONS, 
    "capabilities"     : _ADAPTOR_CAPABILITIES,
    "description"      : """ 
        The SLURM job adaptor. This adaptor uses the SLURM command line tools to run
        remote jobs.
        """,
    "details"          : """ 
        A more elaborate description....
        """,
    "schemas"          : {"slurm"        :"use slurm to run local SLURM jobs", 
                          "slurm+ssh"    :"use ssh to run remote SLURM jobs", 
                          "slurm+gsissh" :"use gsissh to run remote SLURM jobs"}
}

# --------------------------------------------------------------------
# the adaptor info is used to register the adaptor with SAGA

_ADAPTOR_INFO          = {
    "name"             : _ADAPTOR_NAME,
    "version"          : "v0.1",
    "schemas"          : _ADAPTOR_SCHEMAS,
    "cpis"             : [
        { 
        "type"         : "saga.job.Service",
        "class"        : "SLURMJobService"
        }, 
        { 
        "type"         : "saga.job.Job",
        "class"        : "SLURMJob"
        }
    ]
}

###############################################################################
# The adaptor class

class Adaptor (saga.adaptors.cpi.base.AdaptorBase):
    """ 
    This is the actual adaptor class, which gets loaded by SAGA (i.e. by the
    SAGA engine), and which registers the CPI implementation classes which
    provide the adaptor's functionality.
    """


    # ----------------------------------------------------------------
    #
    def __init__ (self) :

        saga.adaptors.cpi.base.AdaptorBase.__init__ (self, _ADAPTOR_INFO, _ADAPTOR_OPTIONS)

        self.id_re = re.compile ('^\[(.*)\]-\[(.*?)\]$')

    # ----------------------------------------------------------------
    #
    def sanity_check (self) :

        # FIXME: also check for gsissh

        pass


    def parse_id (self, id) :
        # split the id '[rm]-[pid]' in its parts, and return them.

        match = self.id_re.match (id)

        if  not match or len (match.groups()) != 2 :
            raise saga.BadParameter ("Cannot parse job id '%s'" % id)

        return (match.group(1), match.group (2))


###############################################################################
#
class SLURMJobService (saga.adaptors.cpi.job.Service) :
    """ Implements saga.adaptors.cpi.job.Service """

    # ----------------------------------------------------------------
    #
    def __init__ (self, api, adaptor) :

        #saga.adaptors.cpi.CPIBase.__init__ (self, api, adaptor)
        self._cpi_base = super  (SLURMJobService, self)
        self._cpi_base.__init__ (api, adaptor)
        self._base = base = "$HOME/.saga/adaptors/slurm_job"

        self.exit_code_re = re.compile("""(?<=ExitCode=)[0-9]*""")



    # ----------------------------------------------------------------
    #
    def __del__ (self) :

        # FIXME: not sure if we should PURGE here -- that removes states which
        # might not be evaluated, yet.  Should we mark state evaluation
        # separately? 
        #   cmd_state () { touch $DIR/purgeable; ... }
        # When should that be done?

      # try :
      #   # if self.shell : self.shell.run_sync ("PURGE", iomode=None)
      #   # self._logger.trace ()
      #   # self._logger.breakpoint ()
      #     if self.shell : self.shell.run_sync ("QUIT" , iomode=None)
      # except :
      #     pass

        try :
            if self.shell : del (self.shell)
        except :
            pass


    # ----------------------------------------------------------------
    #
    @SYNC_CALL
    def init_instance (self, adaptor_state, rm_url, session) :
        """ Service instance constructor """

        self.rm      = rm_url
        self.session = session

        self._open ()


    # ----------------------------------------------------------------
    #
    def _alive (self) :

        if  not self.shell or not self.shell.alive () :
            self._logger.info ("shell is dead - long live the shell")
            
            try :
                self._close ()  # for cleanup...
                self._open  ()

            except Exception :
                # did not work for good - give up
                raise saga.IncorrectState ("job service is not connected, can't reconnect")


    # ----------------------------------------------------------------
    #
    def _open (self) :
        # start the shell, find its prompt.  If that is up and running, we can
        # bootstrap our wrapper script, and then run jobs etc.
        if self.rm.schema   == "slurm":
            shell_schema = "fork://"
        elif self.rm.schema == "slurm+ssh":
            shell_schema = "ssh://"
        elif self.rm.schema == "slurm+gsissh":
            shell_schema = "gsissh://"
        else:
            raise saga.IncorrectURL("Schema %s not supported by SLURM adaptor."
                                    % self.rm.schema)

        #<scheme>://<user>:<pass>@<host>:<port>/<path>?<query>#<fragment>
        # build our shell URL
        shell_url = shell_schema 
        
        # did we provide a username and password?
        if self.rm.username and self.rm.password:
            shell_url += self.rm.username + ":" + self.rm.password + "@"

        # only provided a username
        if self.rm.username and not self.rm.password:
            shell_url += self.rm.username + "@"

        #add hostname
        shell_url += self.rm.host

        #add port
        if self.rm.port:
            shell_url += ":" + self.rm.port

        shell_url = saga.url.Url(shell_url)

        self._logger.debug("Opening shell of type: %s" % shell_url)
        self.shell = saga.utils.pty_shell.PTYShell (shell_url, self.session.contexts, self._logger)

        # -- now stage the shell wrapper script, and run it.  Once that is up
        # and running, we can requests job start / management operations via its
        # stdio.

        base = "$HOME/.saga/adaptors/slurm_job"
        
        ret, out, _ = self.shell.run_sync ("mkdir -p %s" % base)
        if  ret != 0 :
            raise saga.NoSuccess ("failed to prepare base dir (%s)(%s)" % (ret, out))
        self._logger.debug ("got cmd prompt (%s)(%s)" % (ret, out))
        
        # yank out username if it wasn't made explicit
        # TODO: IS MODIFYING THE URL LIKE THIS LEGIT?  if not fix it
        if not self.rm.username:
            self._logger.debug ("No username provided in URL %s, so we are"
                                "going to find it with whoami" % shell_url)
            ret, out, _ = self.shell.run_sync("whoami")
            self.rm.username = out.strip()
            self._logger.debug("Username detected as: %s", self.rm.username)


    # ----------------------------------------------------------------
    #
    def _close (self) :
        del (self.shell)
        self.shell = None


    # ----------------------------------------------------------------
    #
    #
    def _job_run (self, jd) :
        """ runs a job on the wrapper via pty, and returns the job id """
        
        #define a bunch of default args
        exe = jd.executable
        arg = ""
        env = ""
        cwd = ""
        job_name = "SAGAPythonSLURMJob"
        spmd_variation = None
        total_cpu_count = None
        number_of_processes = None
        threads_per_process = None
        working_directory = None
        output = "saga-python-slurm-default.out"
        error = None
        file_transfer = None
        job_start_time = None
        wall_time_limit = None
        queue = None
        project = None
        job_contact = None
        
        # check to see what's available in our job description
        # to override defaults

        if jd.attribute_exists ("name"):
            #TODO: alert user or quit with exception if
            # we have to mangle the name
            job_name = string.replace(jd.name, " ", "_")

        if jd.attribute_exists ("arguments") :
            for a in jd.arguments :
                arg += " %s" % a

        if jd.attribute_exists ("environment") :
            for e in jd.environment :
                env += "export %s=%s; "  %  (e, jd.environment[e])

        if jd.attribute_exists ("spmd_variation"):
            spmd_variation = jd.spmd_variation

        if jd.attribute_exists ("total_cpu_count"):
            total_cpu_count = jd.total_cpu_count

        if jd.attribute_exists ("number_of_processes"):
            number_of_processes = jd.number_of_processes

        if jd.attribute_exists ("processes_per_host"):
            processes_per_host = jd.processes_per_host

        if jd.attribute_exists ("threads_per_process"):
            threads_per_process = jd.threads_per_process

        if jd.attribute_exists ("working_directory"):
            cwd = jd.working_directory

        if jd.attribute_exists ("output"):
            output = jd.output

        if jd.attribute_exists("error"):
            error = jd.error

        if jd.attribute_exists("wall_time_limit"):
            wall_time_limit = jd.wall_time_limit

        if jd.attribute_exists("queue"):
            queue = jd.queue

        if jd.attribute_exists("project"):
            project = jd.project

        if jd.attribute_exists("job_contact"):
            job_contact = jd.job_contact[0]

        slurm_script = "#!/bin/bash\n"

        if job_name:
            slurm_script += '#SBATCH -J %s\n' % job_name

        if spmd_variation:
            pass #TODO

        if total_cpu_count:
            pass

        if number_of_processes:
            slurm_script += "#SBATCH -n %s\n" % number_of_processes

        if threads_per_process:
            pass

        if working_directory:
            pass

        if output:
            slurm_script+= "#SBATCH -o %s\n" % output
        
        if error:
            slurm_script += "#SBATCH -e %s\n" % error

        if wall_time_limit:
            hours = wall_time_limit / 60
            minutes = wall_time_limit % 60
            slurm_script += "#SBATCH -t %s:%s:00\n" % (hours, minutes)

        if queue:
            slurm_script += "#SBATCH -p %s\n" % queue

        if project:
            slurm_script += "#SBATCH -A %s\n" % project

        if job_contact:
            slurm_script += "#SBATCH --mail-user=%s\n" % job_contact
        
        # add on our environment variables
        slurm_script += env + "\n"

        # create our commandline
        slurm_script += exe + arg

        self._logger.debug("SLURM script generated:\n%s" % slurm_script)
        self._logger.debug("Transferring SLURM script to remote host")

        # transfer our script over
        self.shell.stage_to_file (src = slurm_script, 
                                  tgt = "%s/wrapper.sh" % self._base)

        ret, out, _ = self.shell.run_sync("exec %s/wrapper.sh" % self._base)
        ret, out, _ = self.shell.run_sync("sbatch %s/wrapper.sh" % self._base)
        
        # find out what our job ID will be
        # TODO: Could make this more efficient
        found_id = False
        for line in out.split("\n"):
            if "Submitted batch job" in line:
                self.job_id = "[%s]-[%s]" % \
                    (self.rm, int(line.split()[-1:][0]))
                found_id = True

        if not found_id:
            raise saga.NoSuccess._log(self._logger, 
                             "Couldn't get job id from submitted job!")

        self._logger.debug ("started job %s" % self.job_id)

        return self.job_id

    # ----------------  
    # FROM STAMPEDE'S SQUEUE MAN PAGE
    # 
    # JOB STATE CODES
    #    Jobs typically pass through several states in the course of their execution.  The typical states are PENDING, RUNNING, SUSPENDED, COMPLETING, and COMPLETED.   An  explanation  of  each
    #    state follows.

    #    CA  CANCELLED       Job was explicitly cancelled by the user or system administrator.  The job may or may not have been initiated.
    #    CD  COMPLETED       Job has terminated all processes on all nodes.
    #    CF  CONFIGURING     Job has been allocated resources, but are waiting for them to become ready for use (e.g. booting).
    #    CG  COMPLETING      Job is in the process of completing. Some processes on some nodes may still be active.
    #    F   FAILED          Job terminated with non-zero exit code or other failure condition.
    #    NF  NODE_FAIL       Job terminated due to failure of one or more allocated nodes.
    #    PD  PENDING         Job is awaiting resource allocation.
    #    PR  PREEMPTED       Job terminated due to preemption.
    #    R   RUNNING         Job currently has an allocation.
    #    S   SUSPENDED       Job has an allocation, but execution has been suspended.
    #    TO  TIMEOUT         Job terminated upon reaching its time limit.

    # TODO: should CG/CF/CG = done?  that's how it is in the BJ adaptor, but 
    # that doesn't sound right...
    # maybe if there are no jobs we should just mark the job as completed...


    def _slurm_to_saga_jobstate(self, pbsjs):
        """ translates a slurm one-letter state to saga
        """
        if pbsjs == 'CA':
            return saga.job.CANCELED
        elif pbsjs == 'CD':
            return saga.job.DONE
        elif pbsjs == 'CF':
            return saga.job.PENDING
        elif pbsjs == 'CG':
            return saga.job.DONE #TODO: CHECK CORRECTNESS
        elif pbsjs == 'F':
            return saga.job.FAILED
        elif pbsjs == 'NF': #node failure
            return saga.job.FAILED
        elif pbsjs == 'PD':
            return saga.job.PENDING
        elif pbsjs == 'PR':
            return saga.job.CANCELLED #due to preemption
        elif pbsjs == 'R':
            return saga.job.RUNNING
        elif pbsjs == 'S':
            return saga.job.SUSPENDED
        elif pbsjs == 'TO': # timeout
            return saga.job.CANCELED
        else:
            return saga.job.UNKNOWN

    def _job_get_exit_code (self, id) :
        """ get the job exit code from the wrapper shell """
        rm, pid     = self._adaptor.parse_id (id)
        ret, out, _ = self.shell.run_sync("scontrol show job %s" % pid)

        exit_code_found = False
        
        # dig out our exitcode
        for line in out.split("\n"):
            if "ExitCode" in line:
                return self.exit_code_re.search(line).group(0)
        
        # couldn't get the exitcode -- maybe should change this to be
        # None?  b/c we will lose the code if a program waits too
        # long to look for the exitcode...
        raise saga.NoSuccess._log (self._logger, 
                                   "Could not find exit code for job %s" % id)
        return 0

    def _job_cancel (self, id):
        rm, pid     = self._adaptor.parse_id (id)
        ret, out, _ = self.shell.run_sync("scancel %s" % pid)
        if ret == 0:
            return True
        else:
            raise saga.NoSuccess._log(self._logger,
                                      "Could not cancel job %s" % id)


    # ----------------------------------------------------------------
    #
    @SYNC_CALL
    def create_job (self, jd) :
        """ Implements saga.adaptors.cpi.job.Service.get_url()
        """
        # check that only supported attributes are provided
        for attribute in jd.list_attributes():
            if attribute not in _ADAPTOR_CAPABILITIES["jdes_attributes"]:
                msg = "'JobDescription.%s' is not supported by this adaptor" % attribute
                raise saga.BadParameter._log (self._logger, msg)

        
        # this dict is passed on to the job adaptor class -- use it to pass any
        # state information you need there.
        adaptor_state = { "job_service"     : self, 
                          "job_description" : jd,
                          "job_schema"      : self.rm.schema }

        return saga.job.Job (_adaptor=self._adaptor, _adaptor_state=adaptor_state)

    # ----------------------------------------------------------------
    @SYNC_CALL
    def get_url (self) :
        """ Implements saga.adaptors.cpi.job.Service.get_url()
        """
        return self.rm


    # ----------------------------------------------------------------
    #
    @SYNC_CALL
    def list(self):
        """ Implements saga.adaptors.cpi.job.Service.list()
        """

        # ashleyz@login1:~$ squeue -h -o "%i" -u ashleyz                                                                                                                                                                       
        # 255042
        # 255035
        # 255028
        # 255018

        # this line gives us a nothing but jobids for our user
        ret, out, _ = self.shell.run_sync('squeue -h -o "%%i" -u %s' 
                                          % self.rm.username)
        output = ["[%s]-[%s]" % (self.rm, i) for i in out.strip().split("\n")]
        return output
  #
  #
  # # ----------------------------------------------------------------
  # #
  # @SYNC_CALL
  # def get_job (self, jobid):
  #     """ Implements saga.adaptors.cpi.job.Service.get_url()
  #     """
  #     if jobid not in self._jobs.values():
  #         msg = "Service instance doesn't know a Job with ID '%s'" % (jobid)
  #         raise saga.BadParameter._log (self._logger, msg)
  #     else:
  #         for (job_obj, job_id) in self._jobs.iteritems():
  #             if job_id == jobid:
  #                 return job_obj.get_api ()
  #
  #
  # # ----------------------------------------------------------------
  # #
  # def container_run (self, jobs) :
  #     self._logger.debug("container run: %s"  %  str(jobs))
  #     # TODO: this is not optimized yet
  #     for job in jobs:
  #         job.run()
  #
  #
  # # ----------------------------------------------------------------
  # #
  # def container_wait (self, jobs, mode, timeout) :
  #     self._logger.debug("container wait: %s"  %  str(jobs))
  #     # TODO: this is not optimized yet
  #     for job in jobs:
  #         job.wait()
  #
  #
  # # ----------------------------------------------------------------
  # #
  # def container_cancel (self, jobs) :
  #     self._logger.debug("container cancel: %s"  %  str(jobs))
  #     raise saga.NoSuccess("Not Implemented");


###############################################################################
#
class SLURMJob (saga.adaptors.cpi.job.Job):
    """ Implements saga.adaptors.cpi.job.Job
    """
    # ----------------------------------------------------------------
    #
    def __init__ (self, api, adaptor) :

        self._cpi_base = super  (SLURMJob, self)
        self._cpi_base.__init__ (api, adaptor)


    # ----------------------------------------------------------------
    #
    @SYNC_CALL
    def init_instance (self, job_info):
        """ Implements saga.adaptors.cpi.job.Job.init_instance()
        """
        self.jd = job_info["job_description"]
        self.js = job_info["job_service"] 

        # the js is responsible for job bulk operations -- which
        # for jobs only work for run()
        self._container       = self.js
        self._method_type     = "run"

        # initialize job attribute values
        self._id              = None
        self._state           = saga.job.NEW
        self._exit_code       = None
        self._exception       = None
        self._started         = None
        self._finished        = None
        
        return self.get_api ()

    def _job_get_state (self, id) :
        """ get the job state from the wrapper shell """

        # if the state is NEW and we haven't sent out a run comment, keep
        # it listed as NEW
        if self._state == saga.job.NEW and not self._started:
            return saga.job.NEW

        # if we don't even have an ID, state is unknown
        # TODO: VERIFY CORRECTNESS

        if id==None:
            return saga.job.UNKNOWN

        # if the state is DONE, CANCELED or FAILED, it is considered
        # final and we don't need to query the backend again
        if self._state == saga.job.CANCELED or self._state == saga.job.FAILED \
            or self._state == saga.job.DONE:
            return self._state

        rm, pid     = self._adaptor.parse_id (id)
        
        # grab nothing but ID and states
        # output looks like:   
        # 255333 CG
        ret, out, _ = self.js.shell.run_sync('squeue -h -o "%%i %%t" -u %s' % \
                                              self.js.rm.username)

        state_found = False
        
        # no jobs active
        if out.strip().split("\n")==['']:
            return saga.job.UNKNOWN

        for line in out.strip().split("\n"):
            if int(line.split()[0]) == int(pid):
                state_found = True
                return self.js._slurm_to_saga_jobstate(line.split()[1])
                 

        if not state_found:
            return saga.job.UNKNOWN
        #    raise saga.NoSuccess._log (self._logger, 
        #                               "Couldn't find job state")
        
        raise saga.NoSuccess._log (self._logger,
                                   "Internal SLURM adaptor error"
                                   " in _job_get_state")

    # ----------------------------------------------------------------
    #
    @SYNC_CALL
    def get_state(self):
        """ Implements saga.adaptors.cpi.job.Job.get_state()
        """
        self._state = self._job_get_state (self._id)
        return self._state

    # ----------------------------------------------------------------
    #
    @SYNC_CALL
    def get_description (self):
        return self.jd

  

  # # ----------------------------------------------------------------
  # #
    @SYNC_CALL
    def wait(self, timeout):
        time_start = time.time()
        time_now   = time_start
        rm, pid    = self._adaptor.parse_id(self._id)

        while True:
            state = self._job_get_state(self._id)
            self._logger.debug("wait() state for job id %s:%s"%(self._id, state))
            if state == saga.job.DONE or \
               state == saga.job.FAILED or \
               state == saga.job.CANCELED:
                    return True
            # avoid busy poll
            time.sleep(0.5)

            # check if we hit timeout
            if timeout >= 0:
                time_now = time.time()
                if time_now - time_start > timeout:
                    return False

        return True


  #
    # ----------------------------------------------------------------
    #
    # Andre Merzky: In general, the job ID is something which is generated by the adaptor or by the backend, and the user should not interpret it.  So, you can do that.  Two caveats though:
    # (a) The ID MUST remain constant once it is assigned to a job (imagine an application hashes on job ids, for example.
    # (b) the ID SHOULD follow the scheme [service_url]-[backend-id] -- and in that case, you should make sure that the URL part of the ID can be used to create a new job service instance...

    @SYNC_CALL
    def get_id (self) :
        """ Implements saga.adaptors.cpi.job.Job.get_id() """        
        return self._id
   
  # # ----------------------------------------------------------------
  # #
    @SYNC_CALL
    def get_exit_code(self) :
        """ Implements saga.adaptors.cpi.job.Job.get_exit_code()
        """   
        return self.js._job_get_exit_code(self._id)
  #
  # # ----------------------------------------------------------------
  # #
  # @SYNC_CALL
  # def get_created(self) :
  #     """ Implements saga.adaptors.cpi.job.Job.get_started()
  #     """     
  #     # for local jobs started == created. for other adaptors 
  #     # this is not necessarily true   
  #     return self._started
  #
  # # ----------------------------------------------------------------
  # #
  # @SYNC_CALL
  # def get_started(self) :
  #     """ Implements saga.adaptors.cpi.job.Job.get_started()
  #     """        
  #     return self._started
  #
  # # ----------------------------------------------------------------
  # #
  # @SYNC_CALL
  # def get_finished(self) :
  #     """ Implements saga.adaptors.cpi.job.Job.get_finished()
  #     """        
  #     return self._finished
  # 
  # # ----------------------------------------------------------------
  # #
  # @SYNC_CALL
  # def get_execution_hosts(self) :
  #     """ Implements saga.adaptors.cpi.job.Job.get_execution_hosts()
  #     """        
  #     return self._execution_hosts
  #
  # # ----------------------------------------------------------------
  # #
    @SYNC_CALL
    def cancel(self, timeout):
        #scancel id
        self.js._job_cancel(self._id)

  #
  #
    # ----------------------------------------------------------------
    #
    @SYNC_CALL
    def run(self): 
        """ Implements saga.adaptors.cpi.job.Job.run()
        """
        self._id = self.js._job_run (self.jd)
        self._started = True


  # # ----------------------------------------------------------------
  # #
  # @SYNC_CALL
  # def re_raise(self):
  #     return self._exception


# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4

