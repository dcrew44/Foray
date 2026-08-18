# MCP Server
## Initial Job Sites
### Greenhouse, Lever, Ashby

## Design

### JobPosting
Job postings will have different shapes from different sources so the server must define a canonical JobPosting.

#### Properties
    id, 
    title, 
    company, 
    location, 
    salary?, 
    posted_date?, 
    description, 
    source, 
    url

### Tool Surface
Tools should preserve context as best as possible 
#### search_jobs(query, location, limit)
- searches pulls from the cached jobs in the db
- Should only return compacted summaries so they don't fill context window
##### Parameters
query: str - the query used to filter based on job title, jd?, maybe make a structured query it's own class or something
location: str | list[str] | None = None - the location/s to search 
date_filter? 
limit: int - number of jobs to pull

##### Return 
list[truncated_jobpostings]

#### get_job_details(id)
- Should return full job details
- Should strip boiler plate equal opportunity text to preserve context
##### Param
id: str - an id to search the database for the full job posting format greenhouse:acme:12345
    - may want to use a secondary in memory cache to store JobPostings after they are fetched so we aren't having to requery db 
##### Return
JobPosting

#### list_sources()
- Should return the available sources to search for jobs
##### Return
list[str] - list of the different job boards, maybe format similar to id greenhouse:acme  

#### update_job_status(id)
- updates the job status
### State
- Uses saves seen/evaluated/applied jobs
- SQLite
    - Can be used to limit search returned values
    - Strong guarentee of shape of data
    - Fast
- Files managed by agent
    - Seems a bit more agent forward to me
    - Can allow agents to pull the info they need, for example finding similar jobs that were applied to for resume personalization
    - easier to implement?
I lean towards sqlite but not sure
Resume can be a resource
### SQL

### Skills
#### compare_job_to_resume

### Issues
- Greenhouse and others are per company. need to figure out where we get the companies from 
    - Fetch looks like boards-api.greenhouse.io/v1/boards/{company_token}/jobs
    - Ideally, the companies can be saved, to start with we can do a manual config
    - add_company and remove_company tools could be helpful
    - if there are sites that scrape/aggregate greenhouse companies, they could be used to fetch companies by sector
- slow fetching n company job boards
    - cacheing in SQLite can fix this
    - search_job then querys the cache

## Future Ideas
- Red teaming
    - Skills where a customized resume and maybe coverletter is evaluated from the perspective of a recruiter who posted the job.
    - should consider the fit based on the jd, company culture, etc.
    - Should be launched as a subagent.

# NOTES TO SELF ON NON SELF INFLUENCES
- Did not forsee caching issue myself, was a suggestion from claude, important lesson