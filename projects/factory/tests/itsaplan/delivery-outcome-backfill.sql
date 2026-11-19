-- Run against the schema immediately before HOME-246. The launcher rolls
-- this fixture and the trial migration back before the other tests start.
INSERT INTO repo_binding(name, owner, repo, base_branch, itsaplan_project,
    kagent_namespace, kagent_agent, branch_prefix, max_engineering_retries)
VALUES ('fixture', 'org', 'repo', 'main', 'FIX', 'ns', 'coder', 'factory/', 1);

INSERT INTO work_item(id, binding_name, issue_id, issue_key, branch, agent_key,
    kagent_namespace, kagent_agent, state)
VALUES (1,'fixture',1,'FIX-1','one','coder','ns','coder','done'),
       (2,'fixture',2,'FIX-2','two','coder','ns','coder','stopped'),
       (3,'fixture',3,'FIX-3','three','coder','ns','coder','stopped');

INSERT INTO run(id, work_item_id, agent_key, trigger, prompt, system_prompt,
    attempt, state, arm, task_class, base_sha, input_tokens, output_tokens, created_at)
VALUES (1,1,'coder','issue_delegated','','',1,'closed','control','feature','base',100,20,now()),
       (-1,1,'coder','review','','',1,'closed','candidate','other','later',20,10,now()+interval '1 hour'),
       (2,2,'coder','issue_delegated','','',1,'closed','candidate','fix','base',40,10,now()),
       (3,3,'coder','issue_delegated','','',1,'closed','other','fix','base',40,10,now());

INSERT INTO pull_request(work_item_id, number, node_id, head_sha, state, merged_at, merge_commit_sha)
VALUES (1,11,'node-11','head','merged',now()-interval '4 days','merge'),
       (2,12,'node-12','head','open',NULL,NULL);
